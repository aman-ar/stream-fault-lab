# Stream Fault Lab

Break a live stream on purpose, on your Mac, and see how your player copes. Supports WebRTC (WHEP) and HLS / LL-HLS.

```mermaid
flowchart LR
    SRC["🎥 <b>Source</b><br/>OBS · HLS URL · test pattern"]
    ORI["🖥️ <b>Origin</b><br/>holds the live stream"]
    EDG["📡 <b>Edge</b><br/>serves WHEP or HLS"]
    CDN{{"⚡ <b>Mock CDN / network</b><br/>Gateway (HTTP)<br/>Media relay (UDP/TCP)"}}
    PLY["📱 <b>Your player</b>"]
    CTL["🎛️ <b>Fault controls</b><br/>web UI"]

    SRC --> ORI --> EDG --> CDN --> PLY
    CTL -. inject / restore .-> CDN

    classDef healthy fill:#e8f5e9,stroke:#2e7d32,color:#1b5e20
    classDef fault fill:#fff3e0,stroke:#ef6c00,stroke-width:3px,color:#bf360c
    classDef player fill:#e3f2fd,stroke:#1565c0,color:#0d47a1
    classDef ctl fill:#f3e5f5,stroke:#6a1b9a,color:#4a148c
    class SRC,ORI,EDG healthy
    class CDN fault
    class PLY player
    class CTL ctl
```

## Quick start

```sh
brew install python ffmpeg   # once
./start.sh                   # then open http://localhost:9091
```

1. Choose **WHEP** or **HLS**, then **Generated test stream** → **Use this source**.
2. Play in your own player:
   - WHEP: `http://127.0.0.1:8080/live/whep`
   - HLS: `http://127.0.0.1:8080/live/index.m3u8`
3. Pick a fault, then **Apply fault**. **Restore healthy** clears it.

Press **Ctrl+C** to stop everything. For a phone, set `PUBLIC_HOST` to your Mac's LAN IP (see `.env.example`).

## Docs

| Doc | What's in it |
| --- | --- |
| [One-pager](ONE_PAGER.md) | What it tests and how the CDN is mocked |
| [Full reference](docs/REFERENCE.md) | Sources, faults, TURN, logs, ports |
| [Validation](VALIDATION.md) | Recorded checks |
| [Player results](PLAYER_TEST_RESULTS.md) | Observed player behavior |

Local, unauthenticated test tool. Don't expose it publicly.
