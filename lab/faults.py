import asyncio
import math
import time
import uuid


MEDIA_MODES = {'media-outage': 'all', 'rtp-stall': 'rtp', 'video-stall': 'video',
               'audio-stall': 'audio', 'udp-blocked': 'udp', 'network-quality': 'quality'}
CONTROL_PRESETS = set(MEDIA_MODES) | {'edge-down', 'origin-down', 'session-expired'}
HTTP_CODES = {f'http{code}': code for code in (401, 403, 404, 410, 429, 502, 503)}
PRESETS = set(HTTP_CODES) | CONTROL_PRESETS | {'delay', 'timeout', 'ice-unreachable', 'dtls-fingerprint',
                                             'redirect307', 'bad-sdp', 'stale-playlist', 'truncate-media'}
SCOPES = {'whep-post', 'whep-lifecycle', 'hls-playlist', 'hls-media', 'playback'}


def number(value, name, lower, upper, integer=False):
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or not lower <= value <= upper:
        raise ValueError(f'{name} must be between {lower} and {upper}.')
    if integer and int(value) != value:
        raise ValueError(f'{name} must be a whole number.')
    return int(value) if integer else value


class Faults:
    def __init__(self, runtime, events):
        self.runtime, self.events = runtime, events
        self.active = self.timer = None
        self.lock = asyncio.Lock()

    def status(self):
        return dict(self.active) if self.active else None

    def match(self, kind):
        fault = self.active
        if not fault or fault['preset'] in CONTROL_PRESETS:
            return None
        if kind == fault['scope'] or (fault['scope'] == 'playback' and kind != 'whep-lifecycle'):
            return dict(fault)
        return None

    def validate(self, preset, scope, duration, delay_ms=3000, options=None):
        if preset not in PRESETS or scope not in SCOPES:
            raise ValueError('Unknown fault preset or scope.')
        protocol = self.runtime.protocol
        if preset in MEDIA_MODES or preset == 'session-expired':
            if protocol != 'whep' or scope != 'playback':
                raise ValueError('This transport/session fault requires WHEP and playback scope.')
        if preset in ('ice-unreachable', 'dtls-fingerprint', 'bad-sdp', 'redirect307'):
            if protocol != 'whep' or scope != 'whep-post':
                raise ValueError('This answer/redirect fault requires new WHEP POST scope.')
        if preset == 'stale-playlist' and (protocol != 'hls' or scope != 'hls-playlist'):
            raise ValueError('Stale playlist requires HLS playlist scope.')
        if preset == 'truncate-media' and (protocol != 'hls' or scope != 'hls-media'):
            raise ValueError('Truncated media requires HLS media scope.')
        if preset not in ('edge-down', 'origin-down') and ((protocol == 'whep' and scope.startswith('hls-')) or
                                                        (protocol == 'hls' and scope.startswith('whep-'))):
            raise ValueError('Choose a fault scope for the selected playback protocol.')
        number(duration, 'Fault duration', 1, 600)
        number(delay_ms, 'Response delay', 100, 30000)
        if options is not None and not isinstance(options, dict):
            raise ValueError('Fault options must be an object.')
        options = dict(options or {})
        allowed = {'direction', 'loss_pct', 'latency_ms', 'jitter_ms', 'reorder_pct', 'bandwidth_kbps',
                   'burst_packets', 'seed', 'include_control', 'retry_after', 'cut_percent'}
        if set(options) - allowed:
            raise ValueError('Unknown fault option.')
        if options.get('direction', 'both') not in ('both', 'upstream', 'downstream'):
            raise ValueError('Direction must be both, upstream, or downstream.')
        for key, low, high, integer in [('loss_pct', 0, 100, False), ('latency_ms', 0, 2000, False),
                ('jitter_ms', 0, 2000, False), ('reorder_pct', 0, 100, False), ('bandwidth_kbps', 0, 50000, False),
                ('burst_packets', 1, 100, True), ('seed', 0, 2147483647, True), ('retry_after', 1, 300, True),
                ('cut_percent', 1, 99, False)]:
            if key in options:
                options[key] = number(options[key], key, low, high, integer)
        if 'include_control' in options and not isinstance(options['include_control'], bool):
            raise ValueError('include_control must be true or false.')
        if preset == 'network-quality' and not any(options.get(k, 0) for k in
                ('loss_pct', 'latency_ms', 'jitter_ms', 'reorder_pct', 'bandwidth_kbps')):
            raise ValueError('Choose at least one network impairment.')
        return options

    async def apply(self, preset, scope, duration, delay_ms=3000, options=None):
        options = self.validate(preset, scope, duration, delay_ms, options)
        async with self.lock:
            await self._restore()
            fault = dict(id=uuid.uuid4().hex[:12], preset=preset, scope=scope, duration=duration,
                         delay_ms=delay_ms, options=options, expires_at=time.time() + duration)
            self.events.emit('controller', 'fault_requested', f'Apply {preset}', **fault)
            try:
                if preset == 'edge-down':
                    await self.runtime.set_edge(False)
                elif preset == 'origin-down':
                    await self.runtime.set_origin(False)
                elif preset == 'session-expired':
                    await self.runtime.expire_edge_sessions()
                elif preset == 'ice-unreachable':
                    await self.runtime.ice_blackhole.start()
                elif preset in MEDIA_MODES:
                    if preset == 'media-outage' and not options:
                        self.runtime.media_relay.set_blocked(True)
                    else:
                        self.runtime.media_relay.configure(dict(options, mode=MEDIA_MODES[preset]))
                self.active = fault
                self.events.emit('controller', 'fault_applied', f'{preset} active', **fault)
                self.timer = asyncio.create_task(self._expire(fault['id'], duration))
                return self.status()
            except BaseException:
                self.events.emit('controller', 'fault_failed', 'Fault could not be applied', level='ERROR', **fault)
                await self._restore()
                raise

    async def _expire(self, fault_id, duration):
        await asyncio.sleep(duration)
        async with self.lock:
            if self.active and self.active['id'] == fault_id:
                self.events.emit('controller', 'fault_expired', 'Fault duration elapsed', fault_id=fault_id)
                try:
                    await self._restore()
                except Exception as exc:
                    self.events.emit('controller', 'restore_failed', type(exc).__name__, level='ERROR', fault_id=fault_id)

    async def _restore(self):
        if self.timer and self.timer is not asyncio.current_task():
            self.timer.cancel()
            await asyncio.gather(self.timer, return_exceptions=True)
        self.timer = None
        old = self.active
        await self.runtime.ice_blackhole.close()
        self.runtime.media_relay.set_blocked(False)
        if hasattr(self.runtime, 'set_origin') and not self.runtime.alive('origin'):
            await self.runtime.set_origin(True)
        if not self.runtime.alive('edge'):
            await self.runtime.set_edge(True)
        self.active = None
        if old:
            self.events.emit('controller', 'fault_restored', 'Selected output restored; fault cleared', fault_id=old['id'])

    async def restore(self):
        async with self.lock:
            self.events.emit('controller', 'restore_requested', 'Restore healthy requested')
            try:
                await self._restore()
            except Exception as exc:
                self.events.emit('controller', 'restore_failed', type(exc).__name__, level='ERROR')
                raise
            self.events.emit('controller', 'healthy_confirmed', 'Servers running; faults cleared')

    async def close(self):
        if self.timer:
            self.timer.cancel()
            await asyncio.gather(self.timer, return_exceptions=True)
        await self.runtime.ice_blackhole.close()
        self.runtime.media_relay.set_blocked(False)


class FaultSequence:
    """A finite, cancellable experiment in the current lab run, not a scheduler."""
    def __init__(self, faults, events):
        self.faults, self.events = faults, events
        self.task = None
        self.current = None

    def status(self):
        return dict(self.current) if self.current else None

    def validate(self, steps):
        if not isinstance(steps, list) or not 1 <= len(steps) <= 20:
            raise ValueError('A sequence must contain 1–20 steps.')
        clean = []
        for step in steps:
            if not isinstance(step, dict):
                raise ValueError('Each sequence step must be an object.')
            duration, healthy = step.get('duration', 5), step.get('healthy', 10)
            number(healthy, 'Healthy interval', 1, 600)
            preset, scope = step.get('preset', 'media-outage'), step.get('scope', 'playback')
            options = self.faults.validate(preset, scope, duration, step.get('delay_ms', 3000), step.get('options'))
            clean.append(dict(preset=preset, scope=scope, duration=duration, healthy=healthy,
                              delay_ms=step.get('delay_ms', 3000), options=options))
        if sum(s['duration'] + s['healthy'] for s in clean) > 1800:
            raise ValueError('Sequence must finish within 30 minutes.')
        return clean

    async def start(self, steps):
        steps = self.validate(steps)
        await self.stop()
        self.current = dict(id=uuid.uuid4().hex[:12], step=0, total=len(steps), phase='starting')
        self.task = asyncio.create_task(self._run(steps))
        return self.status()

    async def _run(self, steps):
        try:
            for index, step in enumerate(steps):
                self.current.update(step=index + 1, phase='fault', preset=step['preset'])
                self.events.emit('sequence', 'step_started', 'Sequence fault interval', **self.current)
                await self.faults.apply(step['preset'], step['scope'], step['duration'], step['delay_ms'], step['options'])
                await asyncio.sleep(step['duration'] + .05)
                await self.faults.restore()
                self.current['phase'] = 'healthy'
                self.events.emit('sequence', 'healthy_interval', 'Sequence healthy interval', **self.current)
                await asyncio.sleep(step['healthy'])
            self.events.emit('sequence', 'sequence_completed', 'Finite fault sequence completed', **self.current)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.events.emit('sequence', 'sequence_failed', type(exc).__name__, level='ERROR')
            await self.faults.restore()
        finally:
            self.current = None

    async def stop(self):
        if self.task:
            task, self.task = self.task, None
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                self.events.emit('sequence', 'sequence_stopped', 'Sequence cancelled')
        self.current = None
        await self.faults.restore()

    async def close(self):
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.task = self.current = None
