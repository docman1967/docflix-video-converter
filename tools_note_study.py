"""Measure _is_note's behaviour against ground truth from the release's own SDH text.

Ground truth per cue comes from the embedded SDH SRT: a line that begins with
`J“`/`j“` has a LEADING note; one that ends with it has a TRAILING note. Every
other glyph on a non-music cue is a letter.
"""
import struct, sys, os, re, json, collections
import numpy as np
from PIL import Image
sys.path.insert(0, os.path.expanduser('~/scripts/video_converter'))
from modules import music_notes as M

NOTE_TOK = re.compile(r'[Jj][“”"\']')

def parse_srt(p):
    s = open(p, encoding='utf-8', errors='replace').read()
    out = []
    for b in re.split(r'\n\s*\n', s):
        if '-->' not in b: continue
        lines = b.split('\n')
        i = next(j for j, l in enumerate(lines) if '-->' in l)
        t = lines[i].split(' --> ')[0]
        h, m, rest = t.split(':')
        sec = int(h)*3600 + int(m)*60 + float(rest.replace(',', '.'))
        out.append((sec, [l.strip() for l in lines[i+1:] if l.strip()]))
    return out

def sup_index(p):
    data = open(p, 'rb').read()
    pos, n, cur = 0, len(data), {}
    objs = []
    while pos + 13 <= n:
        if data[pos:pos+2] != b'PG': pos += 1; continue
        pts = struct.unpack('>I', data[pos+2:pos+6])[0]/90000.0
        st = data[pos+10]; ss = struct.unpack('>H', data[pos+11:pos+13])[0]
        seg = data[pos+13:pos+13+ss]; pos += 13+ss
        if st == 0x14:
            cur = {}
            for i in range(0, len(seg)-2, 5):
                e = seg[2+i]; y, cr, cb, a = seg[3+i:7+i]
                c, d, ee = y-16, cb-128, cr-128
                cur[e] = (max(0,min(255,int(1.164*c+1.596*ee))),
                          max(0,min(255,int(1.164*c-0.392*d-0.813*ee))),
                          max(0,min(255,int(1.164*c+2.017*d))), a)
        elif st == 0x15 and len(seg) >= 11:
            w = struct.unpack('>H', seg[7:9])[0]; h = struct.unpack('>H', seg[9:11])[0]
            if w and h: objs.append((pts, dict(cur), seg[11:], w, h))
    return objs

def decode(pal, rle, w, h):
    px = bytearray(w*h); pp = dp = 0; L = len(rle)
    while dp < L and pp < w*h:
        b1 = rle[dp]; dp += 1
        if b1: px[pp] = b1; pp += 1; continue
        if dp >= L: break
        b2 = rle[dp]; dp += 1
        if b2 == 0:
            m = pp % w
            if m: pp += w-m
        elif b2 < 0x40: pp += min(b2, w*h-pp)
        elif b2 < 0x80:
            if dp >= L: break
            b3 = rle[dp]; dp += 1; pp += min(((b2 & 0x3F) << 8) | b3, w*h-pp)
        elif b2 < 0xC0:
            if dp >= L: break
            rl = min(b2 & 0x3F, w*h-pp); c = rle[dp]; dp += 1
            px[pp:pp+rl] = bytes([c])*rl; pp += rl
        else:
            if dp + 1 >= L: break
            b3 = rle[dp]; dp += 1; c = rle[dp]; dp += 1
            rl = min(((b2 & 0x3F) << 8) | b3, w*h-pp)
            px[pp:pp+rl] = bytes([c])*rl; pp += rl
    idx = np.frombuffer(bytes(px), dtype=np.uint8)
    P = [np.zeros(256, np.uint8) for _ in range(4)]
    for i, v in pal.items():
        if i < 256:
            for k in range(4): P[k][i] = v[k]
    lum = 0.299*P[0][idx] + 0.587*P[1][idx] + 0.114*P[2][idx]
    gray = (lum*(P[3][idx]/255.0)).clip(0,255).astype(np.uint8)
    img = Image.fromarray(gray.reshape(h, w), mode='L')
    bb = img.getbbox()
    if bb: img = img.crop((max(0,bb[0]-12), max(0,bb[1]-12),
                           min(img.width,bb[2]+12), min(img.height,bb[3]+12)))
    return img

def metrics(blob):
    h, w = blob.shape
    if h < M._MIN_H or w < 4: return None
    rows = blob.sum(axis=1).astype(float)
    mid, bot = rows[h//3:2*h//3], rows[2*h//3:]
    if bot.max() == 0 or not (mid > 0).any(): return None
    band = blob[h//3:2*h//3]
    single = sum(1 for r in band if int(np.diff(np.concatenate(([0], r.view(np.int8), [0]))).clip(min=0).sum()) == 1)
    hw = bot.max()
    bb = blob[2*h//3:]; cols = np.where(bb.any(axis=0))[0]
    if not len(cols): return None
    return dict(aspect=h/w, onestem=single/max(1,len(band)),
                waist=hw/max(1.0, float(np.median(mid[mid>0]))),
                hd_h=hw/h, hd_w=hw/w,
                fill=bb.sum()/max(1, len(bb)*(cols[-1]-cols[0]+1)))

def glyphs(img):
    arr = np.array(img.convert('L')); mask = arr > 100
    lab, n = M._label(mask)
    if n == 0 or n > 400: return []
    ys, xs = np.nonzero(lab); ids = lab[ys, xs]
    o = np.argsort(ids, kind='stable'); ys, xs, ids = ys[o], xs[o], ids[o]
    s = np.searchsorted(ids, np.arange(1,n+1), 'left')
    e = np.searchsorted(ids, np.arange(1,n+1), 'right')
    out = []
    for i,(a,b) in enumerate(zip(s,e), start=1):
        if a >= b: continue
        cy, cx = ys[a:b], xs[a:b]
        box = (int(cx.min()), int(cy.min()), int(cx.max()), int(cy.max()))
        blob = lab[box[1]:box[3]+1, box[0]:box[2]+1] == i
        out.append((box, blob))
    return out

NOTES, LETTERS = [], []
for tag in ('ep1_01', 'ep1_10', 'ep5_10'):
    if not (os.path.exists(tag+'.sup') and os.path.exists(tag+'.srt')): continue
    cues = parse_srt(tag+'.srt')
    objs = sup_index(tag+'.sup')
    optr = 0
    used = 0
    for sec, lines in cues:
        cand = [o for o in objs if abs(o[0]-sec) < 0.6]
        if not cand: continue
        img = decode(*cand[0][1:])
        gs = glyphs(img)
        if not gs: continue
        bands = M._line_bands([g[0] for g in gs], gap=2)
        rows = []
        for (y0,y1) in bands:
            r = sorted([g for g in gs if y0 <= (g[0][1]+g[0][3])/2 <= y1], key=lambda g: g[0][0])
            if r: rows.append(r)
        if len(rows) != len(lines): continue          # only use cleanly aligned cues
        used += 1
        for row, text in zip(rows, lines):
            lead = bool(NOTE_TOK.match(text))
            trail = bool(NOTE_TOK.search(text[-3:]))
            for k, (box, blob) in enumerate(row):
                m = metrics(blob)
                if not m: continue
                m['ep'] = tag; m['x'] = box[0]
                gaps = [row[j+1][0][0]-row[j][0][2] for j in range(len(row)-1)]
                med = float(np.median([g for g in gaps if g >= 0])) if gaps else 1.0
                m['line_med'] = max(1.0, med)
                nb = []
                if k: nb.append(box[0]-row[k-1][0][2])
                if k+1 < len(row): nb.append(row[k+1][0][0]-box[2])
                m['min_gap'] = min(nb) if nb else 999
                m['gap_ratio'] = m['min_gap']/m['line_med']
                is_note = (lead and k == 0) or (trail and k == len(row)-1)
                (NOTES if is_note else LETTERS).append(m)
    print(f"  {tag}: {used} cues aligned")

print(f"\nground truth: {len(NOTES)} notes, {len(LETTERS)} letters")
json.dump({'notes': NOTES, 'letters': LETTERS}, open('note_study.json','w'))

def summarise(name, rows, keys):
    print(f"\n--- {name} (n={len(rows)}) ---")
    for k in keys:
        v = np.array([r[k] for r in rows], dtype=float)
        if not len(v): continue
        print(f"   {k:10} min={v.min():6.2f}  p5={np.percentile(v,5):6.2f}  med={np.median(v):6.2f}"
              f"  p95={np.percentile(v,95):6.2f}  max={v.max():6.2f}")
KEYS = ['aspect','onestem','waist','hd_h','hd_w','fill','gap_ratio']
summarise('REAL NOTES', NOTES, KEYS)
summarise('LETTERS', LETTERS, KEYS)
