# Third-party dependencies

## Vendored hls.js

`web/hls.min.js` is hls.js 1.6.13, from the official npm package:

https://registry.npmjs.org/hls.js/-/hls.js-1.6.13.tgz

The package was verified against its published SHA-512 integrity value:

```text
sha512-hNEzjZNHf5bFrUNvdS4/1RjIanuJ6szpWNfTaX5I6WfGynWXGT7K/YQLYtemSvFExzeMdgdE4SsyVLJbd5PcZA==
```

Copyright and BSD-2-Clause license terms are included in `web/hls.LICENSE`. The vendored file is unmodified.

## Downloaded and installed dependencies

MediaMTX 1.21.2 is downloaded from its official GitHub release by `scripts/bootstrap.py` and verified using hard-coded release SHA-256 digests. Its MIT license is downloaded alongside the executable into `.runtime/bin/LICENSE`. These binaries are excluded from Git and the source ZIP.

Python dependencies are installed from `requirements.txt` into the checkout's private virtual environment; their license files remain in the installed distributions. FFmpeg is taken from the user's local installation when present; otherwise the static build bundled with the `imageio-ffmpeg` package is used, under its own license (it includes GPL components such as libx264). Neither the Python virtual environment nor FFmpeg is distributed in the source package.
