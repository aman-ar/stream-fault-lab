# Player acceptance checklist

This is the next-run checklist, not a pass report. Lab implementation checks are in VALIDATION.md; observed Android results are in PLAYER_TEST_RESULTS.md. Test the installed app build against its configured recovery policy. No Android source changes are included in this expansion.

| ID | Setup / action | Evidence to capture | Result |
| --- | --- | --- | --- |
| MEDIA-01 | Play WHEP; video-only stall 15 seconds | Frames freeze, audio bytes continue, ICE remains connected; stall/rebuffer start/end; first recovered frame | Pending app run |
| MEDIA-02 | Audio-only and all-RTP stall, 15 seconds each | Independent audio/video stall detection; recovery without an invented signaling error | Pending app run |
| QUALITY-01 | 5% loss / burst 3, 100±50 ms delay; then cap below encoded bitrate | Loss, concealment, rendered FPS/freezes, live delay, recovery to healthy behavior | Pending app run |
| BUDGET-01 | Transport outage 5/15/45 seconds and around app policy deadline | Retry delay/budget, ICE/DTLS state, old-path vs new-session recovery, terminal outcome | Pending app run |
| REPEAT-01 | Five rounds: 5 seconds faulty / 10 healthy | Retry reset, no overlapping loop, edge sessions do not accumulate, recovered frames each round | Pending app run |
| EXIT-01 | Delay new WHEP POST 10 seconds; exit/re-enter while pending | Old response cannot replace a new attempt; retries canceled; old resource cleanup | Pending app run |
| HTTP-01 | 429/503 with Retry-After; 401/403/404/410 | Policy-defined backoff/classification; real auth refresh needs integration server | Pending app run |
| SESSION-01 | Close active edge sessions; then HTTP410 on lifecycle calls | Fresh WHEP attempt recovers; PATCH/DELETE handling and bounded cleanup | Pending app run |
| SETUP-01 | HTTP307 preserving POST; invalid SDP; initial ICE / DTLS faults | Negotiation classification, normal recovery-owner policy; keep fingerprint verification | Pending expanded app run |
| SOURCE-01 | Origin outage, then OBS encoder stop/restart | Edge stays up during upstream fault; resumed frames/audio/live position; reconnect OBS if needed | Pending app run |
| HLS-01 | Stale 200 playlist, then restore | Media-sequence/live-position advancement; stale detection and recovery | Pending app run |
| HLS-02 | Truncated media/init response; HTTP404 / 503 media scope | Download error, bounded retry/skip per player policy, restored playback | Pending app run |
| LLHLS-01 | Select LL-HLS; delay/timeout playlist reloads and fail media/parts | _HLS_msn/_HLS_part/_HLS_skip handling, preload/delta recovery, restored live position | Pending app run |
| TURN-01 | Same device/source: VPN off; VPN on direct; VPN on with TURN | Selected candidate pair and TURN transport, frames, loss/freeze time; credentials omitted | Pending controlled device run; field success reported |
| TURN-02 | Separate TURN UDP / TCP / trusted TLS; then unreachable server / rejected credentials | No direct path mistaken for relay; errors/retry/fallback and terminal policy | UDP/TCP lab reference verified; device/TLS acceptance pending |
| HANDOVER-01 | Real device Wi-Fi ↔ mobile, airplane mode, VPN toggle | Reachable endpoint on both networks, changed path/address, rejoin and first frame | Needs representative reachable endpoint |
| CDN-01 | Multiple CDNs / DNS/TLS/certificate changes / token refresh | Real steering/auth/certificate behavior, cancellation and fallback | Needs representative infrastructure |
| ABR-01 | Multi-rendition HLS, fail high variant while low remains healthy | Variant fallback, return to healthy quality, buffer/live delay | Needs multi-rendition source |
| SOAK-01 | Repeated faults over longer sessions; exit/re-enter | Client memory/threads/peers, server sessions, retry state, live delay over time | Pending longer run |

For each run record app build, source/codecs, protocol/profile, device/network/VPN, fault/options/duration, fault start/end UTC, app events, selected ICE pair, retry timing, frozen-frame interval, first recovered frame/audio, session cleanup, and final outcome. A 201 response, AudioStarted, or server ready status alone is not proof of playback.

**Open client issue:** DTLS error 10004 must follow the existing recovery manager's configured policy; requiring a fresh PeerConnection must not bypass its backoff. Preserve DTLS classification and certificate verification. This issue stays noted; it is not fixed by adding lab controls.
