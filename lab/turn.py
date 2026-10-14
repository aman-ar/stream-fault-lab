"""Optional native or external TURN. Credentials stay out of traces/status."""
import asyncio
import ipaddress
import re
import secrets
import shutil
import socket


def valid_host(host):
    if not isinstance(host, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.\-]{0,252}', host):
        raise ValueError('Media host must be a hostname or IPv4 address, without port.')
    if host == '10.0.2.2':
        raise ValueError('TURN needs the Mac LAN IP as media host; 10.0.2.2 is an emulator alias.')
    return host


class Turn:
    def __init__(self, root, events):
        self.root, self.events = root, events
        self.mode, self.media_host, self.servers = 'off', '', []
        self.process = None

    def configuration(self):
        return dict(mode=self.mode, media_host=self.media_host, ice_servers=self.servers)

    def status(self):
        return dict(mode=self.mode, media_host=self.media_host, configured=bool(self.servers),
                    local_running=bool(self.process and self.process.returncode is None),
                    server_count=len(self.servers), installed=bool(shutil.which('turnserver')))

    def mediamtx(self):
        return [dict(url=url, username=server.get('username', ''), password=server.get('credential', ''), clientOnly=True)
                for server in self.servers for url in server['urls']]

    def links(self):
        links = []
        for server in self.mediamtx():
            link = '<' + server['url'] + '>; rel="ice-server"'
            if server['username']:
                link += '; username="' + server['username'] + '"; credential="' + server['password'] + '"; credential-type="password"'
            links.append(link)
        return links

    async def configure(self, data):
        mode = data.get('mode', 'off')
        if mode not in ('off', 'local', 'external'):
            raise ValueError('TURN mode must be off, local, or external.')
        host = valid_host(data.get('media_host', '')) if mode != 'off' else ''
        servers = []
        binary = None
        if mode == 'local':
            try:
                address = ipaddress.IPv4Address(host)
                if address.is_unspecified or address.is_multicast:
                    raise ValueError()
            except ValueError:
                raise ValueError('Local TURN requires a Mac IPv4 address.') from None
            binary = shutil.which('turnserver')
            if not binary:
                raise RuntimeError('Install optional TURN support: brew install coturn')
            # Validate address ownership before stopping an existing configuration.
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                try:
                    probe.bind((host, 0))
                except OSError:
                    raise ValueError('Local TURN media host must belong to this Mac.') from None
            owned = self.mode == 'local' and self.media_host == host and self.process and self.process.returncode is None
            if not owned:
                for kind in (socket.SOCK_DGRAM, socket.SOCK_STREAM):
                    with socket.socket(socket.AF_INET, kind) as probe:
                        try:
                            probe.bind((host, 3478))
                        except OSError:
                            raise ValueError('TURN port 3478 is occupied on this address.') from None
            servers = [dict(urls=[f'turn:{host}:3478?transport=udp', f'turn:{host}:3478?transport=tcp'],
                            username='lab', credential=secrets.token_urlsafe(24))]
        if mode == 'external':
            supplied = data.get('ice_servers')
            if not isinstance(supplied, list) or not 1 <= len(supplied) <= 6:
                raise ValueError('Supply 1–6 ICE server objects.')
            for item in supplied:
                urls = item.get('urls')
                if isinstance(urls, str):
                    urls = [urls]
                if not isinstance(urls, list) or not 1 <= len(urls) <= 3:
                    raise ValueError('ICE server urls must be a URL or list of 1–3 URLs.')
                if any(not isinstance(url, str) or not re.fullmatch(r'(?:stun|turn|turns):[A-Za-z0-9.\-]+(?::[0-9]{1,5})?(?:\?transport=(?:udp|tcp))?', url) for url in urls):
                    raise ValueError('Use a stun:, turn:, or turns: URL with hostname/IPv4 and optional transport.')
                username, credential = item.get('username', ''), item.get('credential', '')
                if any(not isinstance(value, str) or len(value) > 512 or any(ord(c) < 32 or c in '"\\' for c in value) for value in (username, credential)):
                    raise ValueError('Invalid ICE credentials.')
                if any(url.startswith(('turn:', 'turns:')) for url in urls) and not (username and credential):
                    raise ValueError('TURN requires username and credential.')
                servers.append(dict(urls=urls, username=username, credential=credential))
        await self.close()
        self.mode, self.media_host, self.servers = mode, host, servers
        if mode == 'local':
            config = self.root / '.runtime/turnserver.conf'
            config.touch(mode=0o600, exist_ok=True)
            config.chmod(0o600)
            config.write_text('\n'.join(['listening-port=3478', 'listening-ip=' + host, 'relay-ip=' + host,
                'min-port=49160', 'max-port=49200', 'realm=stream-fault-lab', 'lt-cred-mech',
                'user=lab:' + servers[0]['credential'], 'no-tls', 'no-dtls', 'no-tcp-relay',
                'no-multicast-peers', 'user-quota=16', 'total-quota=32', 'log-file=/dev/null',
                'no-cli', 'no-stdout-log', 'pidfile=' + str(self.root / '.runtime/turnserver.pid'),
                *(['allow-loopback-peers'] if address.is_loopback else [])]) + '\n')
            self.process = await asyncio.create_subprocess_exec(binary, '-c', str(config),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await asyncio.sleep(.3)
            if self.process.returncode is not None:
                await self.close()
                self.mode, self.media_host, self.servers = 'off', '', []
                raise RuntimeError('Local TURN could not start. Check that port 3478 and relay ports 49160–49200 are free.')
        self.events.emit('turn', 'turn_configured', 'TURN configuration changed; credentials excluded', **self.status())

    async def close(self):
        process, self.process = self.process, None
        if process and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 3)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        path = self.root / '.runtime/turnserver.conf'
        if path.exists():
            path.unlink()
