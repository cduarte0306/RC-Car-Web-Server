// src/server.js
import path from 'path';
import { fileURLToPath } from 'url';
import Fastify from 'fastify';
import fastifyStatic from '@fastify/static';
import { execFile } from 'child_process';
import { promisify } from 'util';

const execFileAsync = promisify(execFile);
const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const app = Fastify({ logger: false });

// Allow overriding interface via env, else auto (nmcli will choose)
const WIFI_IFACE = process.env.WIFI_IFACE || null;

// Serve ./src/public
app.register(fastifyStatic, {
  root: path.join(__dirname, 'public'),
  prefix: '/', // index.html will be served at /
});

// Health
app.get('/healthz', async () => ({ ok: true, ts: Date.now() / 1000 }));

// Scan Wi‑Fi
app.get('/api/wifi/scan', async (req, reply) => {
  try {
    // Fields: SSID,SECURITY,SIGNAL,DEVICE
    // -t = terse, -f = fields, device wifi list = scan (or use 'rescan yes' if needed)
    const args = ['-t', '-f', 'SSID,SECURITY,SIGNAL,DEVICE', 'device', 'wifi', 'list'];
    const { stdout } = await execFileAsync('nmcli', args, { timeout: 8000 });

    const networks = stdout
      .trim()
      .split('\n')
      .filter(Boolean)
      .map(line => {
        // nmcli -t uses ':' as delimiter; SSIDs can also contain ':' so use --fields order
        // Format generally: SSID:SECURITY:SIGNAL:DEVICE
        const parts = line.split(':');
        const device = parts.pop();
        const signal = Number(parts.pop());
        const security = parts.pop(); // may be "--" for open
        const ssid = parts.join(':'); // rejoin remaining as SSID
        return {
          ssid: ssid || '',
          security: security || '--',
          signal: Number.isFinite(signal) ? signal : null,
          iface: device || null,
        };
      });

    return reply.send(networks);
  } catch (err) {
    req.log.error(err);
    return reply.code(500).send({ error: 'nmcli scan failed' });
  }
});

// Connect to Wi‑Fi
app.post('/api/wifi/connect', async (req, reply) => {
  const { ssid, password } = req.body || {};
  if (!ssid) return reply.code(400).send({ error: 'ssid required' });

  try {
    const args = ['device', 'wifi', 'connect', ssid];
    if (WIFI_IFACE) { args.push('ifname', WIFI_IFACE); }
    if (password) { args.push('password', password); }

    // Run nmcli connect
    await execFileAsync('nmcli', args, { timeout: 15000 });

    // Query IP of the interface after connect
    const iface = WIFI_IFACE || await resolveWifiIface(ssid).catch(() => null);
    let ip = null;
    if (iface) {
      try {
        const { stdout } = await execFileAsync('nmcli', ['-t', '-f', 'IP4.ADDRESS', 'device', 'show', iface]);
        const line = stdout.trim().split('\n').find(l => l.startsWith('IP4.ADDRESS'));
        if (line) ip = line.split('=')[1]?.split('/')[0] || null;
      } catch {}
    }

    return reply.send({ ok: true, iface: iface || WIFI_IFACE || null, ip });
  } catch (err) {
    // Surface nmcli stderr if possible
    const msg = err?.stderr?.toString()?.trim() || err.message || 'connect failed';
    return reply.code(500).send({ error: msg });
  }
});

async function resolveWifiIface(ssid) {
  // Find the active Wi‑Fi device connected to SSID
  const { stdout } = await execFileAsync('nmcli', ['-t', '-f', 'NAME,DEVICE,TYPE,STATE', 'connection', 'show', '--active']);
  const line = stdout.trim().split('\n').find(l => l.startsWith(`${ssid}:`));
  if (!line) return null;
  const parts = line.split(':');
  // NAME:DEVICE:TYPE:STATE
  return parts[1] || null;
}

// Start server
const PORT = process.env.PORT || 8000;
const HOST = process.env.HOST || '0.0.0.0';

app.listen({ host: HOST, port: PORT })
  .then(() => console.log(`UI http://${HOST}:${PORT}  •  WIFI_IFACE=${WIFI_IFACE ?? 'auto'}`))
  .catch(err => { console.error(err); process.exit(1); });
