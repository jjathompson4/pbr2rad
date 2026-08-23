# Preview assets

`studio_small_09_512.hdr` — the image-based lighting environment for the web
preview sphere (`web/preview.py`, rig v7): Poly Haven's **Studio Small 09** HDRI
(https://polyhaven.com/a/studio_small_09), CC0, downsampled from the 1k release
to 512 × 256 with Radiance `pfilt -x 512 -y 256`. Used as a `colorpict` pattern
on two glow hemispheres, so chrome reflects a real studio and diffuse materials
are lit by the same environment (white-balanced and rotated in `preview.py`).
