# Synthetic EER encoder (EER v1: 7-bit run-length + 2+2 sub-pixel bits, LSB-first),
# round trip via imagecodecs.eer_decode and tifffile (compression 65001).
# Written during the 2026-11-10 research (see docs/research/cryoem-live.md section 1),
# verified bit-exact with imagecodecs 2026.10.10 + tifffile 2026.11.8.
# Basis for the EER test fixtures (M1). Usage: python eer_synthetic_encoder.py out.eer
# Synthetic EER (v1: 7-bit run length + 4 subpixel bits, LSB-first) -> imagecodecs.eer_decode and tifffile round trip
import numpy as np, imagecodecs, tifffile, sys, time
H=W=256
rng=np.random.default_rng(0)
def encode(positions, subpix):
    bits=[]
    prev=-1
    for p,s in zip(positions,subpix):
        skip=p-prev-1
        while skip>=127:
            bits.append((127,7)); skip-=127
        bits.append((skip,7)); bits.append((s,4)); prev=p
    skip=H*W-prev-1  # final run must land exactly on npixels
    while skip>=127:
        bits.append((127,7)); skip-=127
    bits.append((skip,7))
    # pack LSB-first
    acc=0; n=0; out=bytearray()
    for v,nb in bits:
        acc|=v<<n; n+=nb
        while n>=8: out.append(acc&255); acc>>=8; n-=8
    if n: out.append(acc&255)
    if len(out)%2: out.append(0)  # decoder requires even byte count
    return bytes(out)
frames=[]; truth=np.zeros((H,W),np.int64)
segs=[]
for f in range(40):
    pos=np.sort(rng.choice(H*W, 600, replace=False)); sp=rng.integers(0,16,len(pos))
    data=encode(pos,sp); segs.append(data)
    t=np.zeros(H*W,np.int64); t[pos]=1; truth+=t.reshape(H,W)
    d=imagecodecs.eer_decode(data,(H,W),7,2,2)
    assert (d.astype(np.int64)==t.reshape(H,W)).all(), "mismatch"
print("eer_decode direct OK, dtype", d.dtype)
d4=imagecodecs.eer_decode(segs[0],(H*4,W*4),7,2,2,superres=2); print("superres=2 shape",d4.shape,"events",int(d4.sum()))
# write a multi-page TIFF with compression 65001 and the EER acquisition XML tag
xml=b'<metadata><item name="numberOfFrames">40</item><item name="exposureTime" unit="s">4.0</item><item name="sensorPixelSize.width" unit="m">1.4e-10</item></metadata>'
fn=sys.argv[1]
import struct
def write_eer(fn, segs, H, W, xml):
    # minimal little-endian TIFF, one strip per page, compression 65001 (EER 7-bit)
    buf=bytearray(b'II*\x00'+b'\x00'*4)
    ifd_ptr_pos=4
    xmloff=len(buf); buf+=xml
    for i,sg in enumerate(segs):
        if len(buf)%2: buf.append(0)
        off=len(buf); buf+=sg
        if len(buf)%2: buf.append(0)
        tags=[(256,4,1,W),(257,4,1,H),(258,3,1,8),(259,3,1,65001),(262,3,1,1),(273,4,1,off),(277,3,1,1),(278,4,1,H),(279,4,1,len(sg))]
        if i==0: tags.append((65001,7,len(xml),xmloff))
        ifd=len(buf); struct.pack_into('<I',buf,ifd_ptr_pos,ifd)
        buf+=struct.pack('<H',len(tags))
        for code,typ,cnt,val in tags:
            if typ==3: buf+=struct.pack('<HHIHH',code,typ,cnt,val,0)
            else: buf+=struct.pack('<HHII',code,typ,cnt,val)
        ifd_ptr_pos=len(buf); buf+=b'\x00'*4
    open(fn,'wb').write(buf)
write_eer(fn, segs, H, W, xml)
with tifffile.TiffFile(fn, is_eer=True) as tf:
    print("is_eer",tf.is_eer, "pages",len(tf.pages), "meta",tf.eer_metadata)
    t0=time.time(); s=np.zeros((H,W),np.uint32)
    for p in tf.pages: s+=p.asarray()
    print("sum OK", (s==truth).all(), f"{time.time()-t0:.3f}s")
with tifffile.TiffFile(fn, superres=1, is_eer=True) as tf:
    print("superres=1 page shape", tf.pages[0].asarray().shape)
