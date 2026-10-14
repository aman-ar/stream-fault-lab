# Advanced: TURN, VPN and LL-HLS

Optional checks. The basic lab works without any of this.

## TURN and VPN checks

Use these to tell whether playback works through a relay, for example behind a VPN.

```sh
brew install coturn   # only for the local TURN option
```

1. **Restore healthy.** Under **TURN / VPN checks**, choose **Local coturn** and enter your Mac's LAN IPv4 address, then configure TURN. The edge restarts, so existing WHEP sessions close.
2. **Copy ICE config** into your app before it creates its offer. The gateway also sends `Link: rel="ice-server"` headers on OPTIONS and POST; your WHEP client must read them.
3. In the reference player, choose **Require TURN over UDP** or **TCP**, then Play. Check the **TURN route** readout and that frames advance.
4. Repeat on your device with the VPN off and on. Save client stats and logs, not just successful signaling.

For negative cases, use an external server that is unreachable or rejects credentials, and check your player's normal error handling.

- **Local coturn:** listens on your Mac IP, UDP/TCP 3478, relay UDP 49160–49200. The lab starts and stops it. Credentials are generated per run, kept in private gitignored files, and left out of status, traces and browser storage.
- **External STUN / TURN / TURN-TLS:** paste RTCConfiguration `iceServers` JSON. TURN/TLS needs a reachable `turns:` service with a trusted certificate.
- **Chrome labeling:** through the media proxy, Chrome may label the selected candidate **prflx** instead of **relay**. The readout identifies the TURN route separately, and relay-only mode stops a direct path passing as TURN.
- **Limits:** local TURN doesn't create public routing or a trusted TLS endpoint. A LAN lab may be unreachable from a VPN or mobile network. TURN settings last for one run; the playback protocol is saved across restarts.

## LL-HLS profile

Select HLS, then **LL-HLS · 200 ms parts**. The edge restarts in MediaMTX's low-latency mode and keeps the source. The gateway preserves `_HLS_msn`, `_HLS_part` and `_HLS_skip`, and rewrites part, map and preload-hint URIs so faults apply to them.

- Test blocked playlist reloads with playlist delay/timeout, and partial segments with HLS media faults.
- Stale-playlist mode holds the first successful snapshot per path. Truncated mode sends part of a response, then closes the connection.
- These are fault fixtures, not a CDN cache.
- Local LL-HLS uses plain HTTP, while Apple's requirements include HTTPS. A single rendition can't test ABR fallback.
