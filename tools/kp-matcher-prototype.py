#!/usr/bin/env python3
"""形态B 第1步: 离线关键点匹配器原型 v2 (纯 stdlib)。

v2 相对 v1 的修正（全部来自实测）:
- 脊距定谳: 88x88@508DPI 水平自相关 lag=8 峰值 → 脊线周期 ≈ 8px
- PATCH 8→16 (含 2 条脊线, SIFT 惯例 patch ≈ 2× 周期)
- DoG sigma 对齐周期: (2.0, 2.8, 4.0, 5.7) ≈ 周期/2 起, √2 步进
- 描述子主方向对齐 (patch 梯度直方图峰值), 抗小旋转
- Lowe ratio 0.85

DoG 关键点 + 16x16 patch 梯度直方图描述子(4x4 cells × 8 bins = 128 维)
+ Lowe 比率检验 + RANSAC 平移模型。
输入: <样本目录>/{same,diff}/*.pgm
输出: 同指/异指分布 + 阈值扫描。

用法: python3 kp-matcher-prototype.py <样本目录>
"""
import os, sys, math, random
from itertools import combinations

W = H = 88
PATCH = 16
HALF = PATCH // 2
SIGMAS = (2.0, 2.8, 4.0, 5.7)   # 脊线周期 8px 对齐

def read_pgm(path):
    with open(path, "rb") as f:
        d = f.read()
    parts = []; i = 2
    while len(parts) < 3:
        while i < len(d) and d[i:i+1] in b" \t\r\n": i += 1
        j = i
        while j < len(d) and d[j:j+1] not in b" \t\r\n": j += 1
        parts.append(d[i:j]); i = j
    i += 1
    return list(d[i:i+88*88])

def gauss_blur(px, sigma):
    radius = max(1, int(2.5 * sigma + 0.5))
    ksize = 2 * radius + 1
    g = [math.exp(-(x * x) / (2 * sigma * sigma)) for x in range(-radius, radius + 1)]
    s = sum(g); g = [v / s for v in g]
    tmp = [0.0] * (W * H)
    for y in range(H):
        row = y * W
        for x in range(W):
            acc = 0.0
            for k in range(ksize):
                xx = x + k - radius
                if 0 <= xx < W: acc += g[k] * px[row + xx]
            tmp[row + x] = acc
    out = [0.0] * (W * H)
    for y in range(H):
        for x in range(W):
            acc = 0.0
            for k in range(ksize):
                yy = y + k - radius
                if 0 <= yy < H: acc += g[k] * tmp[yy * W + x]
            out[y * W + x] = acc
    return out

def quality_gate(px):
    tc = []
    for y in range(1, H - 1):
        for x in range(1, W - 1):
            vals = [px[(y + dy) * W + x + dx] for dy in (-1, 0, 1) for dx in (-1, 0, 1)]
            m = sum(vals) / 9
            tc.append(math.sqrt(sum((t - m) ** 2 for t in vals) / 9))
    cov = sum(1 for t in tc if t > 4) / len(tc)
    return (cov >= 0.70 and sum(tc) / len(tc) >= 15), cov, sum(tc) / len(tc)

def detect_keypoints(px, thresh=3.0, grid=8):
    layers = [gauss_blur(px, s) for s in SIGMAS]
    dogs = [[layers[i + 1][k] - layers[i][k] for k in range(W * H)] for i in range(3)]
    pts = []
    for li in range(3):
        dog = dogs[li]
        dprev, dnext = dogs[max(li - 1, 0)], dogs[min(li + 1, 2)]
        same = (li == 0) or (li == 2)
        for y in range(2, H - 2):
            for x in range(2, W - 2):
                v = dog[y * W + x]
                if abs(v) < thresh: continue
                is_max = True; is_min = True
                for dy in (-1, 0, 1):
                    for dx in (-1, 0, 1):
                        if dy == 0 and dx == 0: continue
                        nv = dog[(y + dy) * W + x + dx]
                        if nv >= v: is_max = False
                        if nv <= v: is_min = False
                if not (is_max or is_min): continue
                if not same:
                    stop = False
                    for l in (dprev, dnext):
                        nv = l[y * W + x]
                        if is_max and nv >= v: is_max = False; stop = True
                        if is_min and nv <= v: is_min = False; stop = True
                        if stop: break
                if not (is_max or is_min): continue
                pts.append((x, y, v, li))
    buckets = {}
    for p in pts:
        key = (p[0] // grid, p[1] // grid)
        buckets.setdefault(key, []).append(p)
    kept = []
    for key, lst in buckets.items():
        lst.sort(key=lambda p: -abs(p[2]))
        kept.extend(lst[:2])
    kept.sort(key=lambda p: (p[1], p[0]))
    return kept

def patch_orient(px, ox, oy):
    hist = [0.0] * 8
    for py in range(PATCH):
        for pxx in range(PATCH):
            gx = px[(oy+py)*W + ox+pxx+1] - px[(oy+py)*W + ox+pxx-1] \
                if 0 < ox+pxx < W-1 else 0
            gy = px[(oy+py+1)*W + ox+pxx] - px[(oy+py-1)*W + ox+pxx] \
                if 0 < oy+py < H-1 else 0
            mag = math.hypot(gx, gy)
            if mag < 1e-6: continue
            ang = int(((math.atan2(gy, gx) + math.pi) / (2 * math.pi)) * 8) % 8
            hist[ang] += mag
    return max(range(8), key=lambda b: hist[b])

def describe(px, kp):
    x, y = kp[0], kp[1]
    ox, oy = x - HALF, y - HALF
    if ox < 1 or oy < 1 or ox + PATCH > W - 1 or oy + PATCH > H - 1:
        return None
    ob = patch_orient(px, ox, oy)
    desc = [0.0] * 128
    for py in range(PATCH):
        for pxx in range(PATCH):
            gx = px[(oy+py)*W + ox+pxx+1] - px[(oy+py)*W + ox+pxx-1]
            gy = px[(oy+py+1)*W + ox+pxx] - px[(oy+py-1)*W + ox+pxx]
            mag = math.hypot(gx, gy)
            ang = int(((math.atan2(gy, gx) + math.pi) / (2 * math.pi)) * 8) % 8
            ang = (ang - ob) % 8
            cell = (py // 4) * 4 + (pxx // 4)
            desc[cell * 8 + ang] += mag
    n = math.sqrt(sum(d * d for d in desc))
    if n < 1e-6: return None
    return [d / n for d in desc]

_feat = {}
def extract(path):
    if path in _feat: return _feat[path]
    px = read_pgm(path)
    feats = []
    for kp in detect_keypoints(px):
        d = describe(px, kp)
        if d: feats.append((kp, d))
    _feat[path] = feats
    return feats

def desc_dist(a, b):
    s = 0.0
    for i in range(128):
        d = a[i] - b[i]
        s += d * d
    return math.sqrt(s)

def match_feats(fA, fB, ratio=0.85):
    matches = []
    for i, (kpA, dA) in enumerate(fA):
        dists = [(desc_dist(dA, dB), j) for j, (kpB, dB) in enumerate(fB)]
        dists.sort()
        if len(dists) < 2: continue
        d1, j1 = dists[0]; d2, _ = dists[1]
        if d2 > 0 and d1 / d2 < ratio:
            matches.append((i, j1, d1))
    return matches

def ransac_translation(fA, fB, matches, iters=60, tol=2.5, min_inliers=4):
    """v2 平移模型（保留用于对比）。"""
    if len(matches) < min_inliers: return 0, None
    rng = random.Random(42)
    best_in, best_t = 0, None
    for _ in range(iters):
        i, j, _ = rng.choice(matches)
        dx = fB[j][0][0] - fA[i][0][0]
        dy = fB[j][0][1] - fA[i][0][1]
        if abs(dx) > 10 or abs(dy) > 10: continue
        inl = 0
        for (ii, jj, _) in matches:
            ex = fB[jj][0][0] - fA[ii][0][0] - dx
            ey = fB[jj][0][1] - fA[ii][0][1] - dy
            if abs(ex) <= tol and abs(ey) <= tol: inl += 1
        if inl > best_in:
            best_in, best_t = inl, (dx, dy)
    return best_in, best_t

def ransac_similarity(fA, fB, matches, iters=200, tol=2.5):
    """v3 相似变换模型（旋转+平移+±30% 尺度），2 点采样。
    2026-10-07 实测：跨时间同指对从平移模型的 0-14 内点提升到 2-68。"""
    if len(matches) < 4: return 0
    rng = random.Random(42)
    best = 0
    for _ in range(iters):
        (i1, j1, _), (i2, j2, _) = rng.sample(matches, 2)
        ax1, ay1 = fA[i1][0][0], fA[i1][0][1]
        ax2, ay2 = fA[i2][0][0], fA[i2][0][1]
        bx1, by1 = fB[j1][0][0], fB[j1][0][1]
        bx2, by2 = fB[j2][0][0], fB[j2][0][1]
        dax, day = ax2 - ax1, ay2 - ay1
        dbx, dby = bx2 - bx1, by2 - by1
        na = math.hypot(dax, day); nb = math.hypot(dbx, dby)
        if na < 1e-6 or nb < 1e-6: continue
        if nb / na > 1.3 or nb / na < 0.77: continue
        ang = math.atan2(dby, dbx) - math.atan2(day, dax)
        c, s = math.cos(ang), math.sin(ang)
        inl = 0
        for (ii, jj, _) in matches:
            x = fA[ii][0][0] - ax1; y = fA[ii][0][1] - ay1
            rx = c * x - s * y + bx1; ry = s * x + c * y + by1
            if abs(rx - fB[jj][0][0]) <= tol and abs(ry - fB[jj][0][1]) <= tol:
                inl += 1
        best = max(best, inl)
    return best

def norm_score(pa, pb):
    """v3 最终打分：相似 RANSAC 内点 / min(特征数) × 100。
    2026-10-07 留一验证（9 帧模板）：真指 10/10（min 13.1），
    异指 0/4 误识（max 3.1），4.2 倍分界，阈值 7。"""
    fa, fb = extract(pa), extract(pb)
    if len(fa) < 6 or len(fb) < 6: return 0.0
    m = match_feats(fa, fb, ratio=0.9)
    inl = ransac_similarity(fa, fb, m)
    return 100.0 * inl / min(len(fa), len(fb))

def verify(template_paths, probe_path, threshold=7.0):
    """verify 模拟：模板多帧取 top1。"""
    scores = [norm_score(t, probe_path) for t in template_paths]
    return max(scores) >= threshold, max(scores) if scores else 0.0

def score_pair(pathA, pathB):
    fA, fB = extract(pathA), extract(pathB)
    if len(fA) < 6 or len(fB) < 6: return None
    m = match_feats(fA, fB)
    inl, t = ransac_translation(fA, fB, m)
    return dict(nA=len(fA), nB=len(fB), nmatch=len(m), inliers=inl)

def main():
    base = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser(
        "~/oc-tmp/calib-20261007")
    same = [f"{base}/same/{f}" for f in sorted(os.listdir(f"{base}/same"))]
    diff = [f"{base}/diff/{f}" for f in sorted(os.listdir(f"{base}/diff"))]

    print("== 每图特征统计 ==")
    for p in same + diff:
        ok, cov, mtc = quality_gate(read_pgm(p))
        f = extract(p)
        print(f"  {os.path.basename(p):16s} gate={'PASS' if ok else 'REJ '} "
              f"feats={len(f):3d} cov={cov:.2f} mtc={mtc:.1f}")

    print()
    print("== 同指 × 同指 (%d 对) ==" % (len(same) * (len(same) - 1) // 2))
    res_same = []
    for a, b in combinations(same, 2):
        s = score_pair(a, b)
        if s: res_same.append((s["inliers"], os.path.basename(a)[:3], os.path.basename(b)[:3]))
    res_same.sort()
    for name, v in [("min", 0), ("p25", len(res_same)//4), ("med", len(res_same)//2), ("p75", 3*len(res_same)//4), ("max", -1)]:
        print(f"  inliers {name}: {res_same[v][0]:3d} ({res_same[v][1]}-{res_same[v][2]})")

    print()
    print("== 同指 × 异指 (%d 对) ==" % (len(same) * len(diff)))
    res_cross = []
    for a in same:
        for b in diff:
            s = score_pair(a, b)
            if s: res_cross.append((s["inliers"], os.path.basename(a)[:3], os.path.basename(b)[:3]))
    res_cross.sort()
    for name, v in [("min", 0), ("p50", len(res_cross)//2), ("p95", int(len(res_cross)*0.95)), ("max", -1)]:
        print(f"  inliers {name}: {res_cross[v][0]:3d} ({res_cross[v][1]}-{res_cross[v][2]})")

    sv = sorted(r[0] for r in res_same)
    cv = sorted(r[0] for r in res_cross)
    print()
    print(f"分界: 同指 min {sv[0]} vs 异指 max {cv[-1]} → 余量 {sv[0] - cv[-1]:+d}")
    print(f"建议阈值: 异指 p95 {cv[int(len(cv)*0.95)]} 与 同指 p25 {sv[len(sv)//4]} 之间")

    print()
    print("== 阈值扫描 (同指命中率 / 异指误识率) ==")
    for th in range(0, 31, 2):
        hit = sum(1 for r in res_same if r[0] >= th) / len(res_same)
        far = sum(1 for r in res_cross if r[0] >= th) / len(res_cross)
        print(f"  th={th:2d}: 同指命中 {hit*100:5.1f}%  异指误识 {far*100:5.1f}%")

if __name__ == "__main__":
    main()
