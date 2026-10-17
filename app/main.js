// goat desktop: an Electron shell that starts the Python supervisor (scraper, worker, bot, mail,
// watchdog, dashboard) and shows the dashboard. Closing the window stops the supervisor.
const { app, BrowserWindow, Menu, shell, dialog, Tray, nativeImage } = require('electron');
const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');
const http = require('http');

const ROOT = path.resolve(__dirname, '..');
const PY = path.join(ROOT, '.venv', 'Scripts', 'python.exe');
const PORT = readEnvPort();
const URL_ = `http://127.0.0.1:${PORT}/`;
let child = null;
let win = null;
let tray = null;
let logBuf = [];

function readEnvPort() {
  try {
    const env = fs.readFileSync(path.join(ROOT, '.env'), 'utf8');
    const m = env.match(/^DASHBOARD_PORT=(\d+)/m);
    return m ? parseInt(m[1], 10) : 8787;
  } catch (e) { return 8787; }
}

function log(line) {
  logBuf.push(line); if (logBuf.length > 2000) logBuf = logBuf.slice(-1500);
  try { fs.appendFileSync(path.join(ROOT, 'records', 'desktop.log'), line + '\n'); } catch (e) {}
}

function ping(cb) {
  const req = http.get(URL_ + 'api/overview', res => { cb(res.statusCode === 200); res.resume(); });
  req.on('error', () => cb(false));
  req.setTimeout(1500, () => { req.destroy(); cb(false); });
}

function startSupervisor() {
  if (child) return;
  if (!fs.existsSync(PY)) {
    dialog.showErrorBox('goat', `Python venv not found at ${PY}. Run the setup in README.md first.`);
    return;
  }
  const args = ['-m', 'goat', 'run'];
  child = spawn(PY, args, { cwd: ROOT, env: { ...process.env, PYTHONUNBUFFERED: '1' }, windowsHide: true });
  child.stdout.on('data', d => log(d.toString().trimEnd()));
  child.stderr.on('data', d => log(d.toString().trimEnd()));
  child.on('exit', code => { log(`supervisor exited ${code}`); child = null; if (win && !win.isDestroyed()) win.setTitle(`goat (stopped, exit ${code})`); });
}

function stopSupervisor() {
  if (!child) return;
  try { child.kill(); } catch (e) {}
  child = null;
}

function createWindow() {
  win = new BrowserWindow({
    width: 1380, height: 900, title: 'goat', backgroundColor: '#0f1115',
    webPreferences: { contextIsolation: true, nodeIntegration: false },
  });
  win.setMenuBarVisibility(false);
  win.webContents.setWindowOpenHandler(({ url }) => { shell.openExternal(url); return { action: 'deny' }; });
  win.on('closed', () => { win = null; });
  // Wait for the dashboard to answer, then load it.
  let tries = 0;
  const wait = () => ping(ok => {
    if (ok) { win.loadURL(URL_); win.setTitle('goat'); return; }
    if (++tries > 60) { win.loadURL('data:text/html,' + encodeURIComponent(`<body style="font-family:sans-serif;background:#0f1115;color:#eee;padding:24px"><h2>goat did not start</h2><pre>${logBuf.slice(-40).join('\n').replace(/</g, '&lt;')}</pre></body>`)); return; }
    win.setTitle(`goat (starting… ${tries})`);
    setTimeout(wait, 1000);
  });
  wait();
}

const menu = Menu.buildFromTemplate([
  { label: 'goat', submenu: [
    { label: 'Reload dashboard', accelerator: 'CmdOrCtrl+R', click: () => win && win.loadURL(URL_) },
    { label: 'Restart agent', click: () => { stopSupervisor(); setTimeout(startSupervisor, 800); } },
    { label: 'Open records folder', click: () => shell.openPath(path.join(ROOT, 'records')) },
    { label: 'Open .env', click: () => shell.openPath(path.join(ROOT, '.env')) },
    { label: 'Show agent log', click: () => dialog.showMessageBox({ message: logBuf.slice(-60).join('\n') || '(no output yet)' }) },
    { type: 'separator' },
    { role: 'toggleDevTools' },
    { role: 'quit' },
  ] },
]);
Menu.setApplicationMenu(menu);

app.whenReady().then(() => {
  // If a supervisor (or `goat dashboard`) already answers on the port, attach to it instead of starting another.
  ping(ok => { if (!ok) startSupervisor(); else log('attached to an already-running goat on ' + URL_); createWindow(); });
  try {
    tray = new Tray(nativeImage.createEmpty());
    tray.setToolTip('goat');
    tray.setContextMenu(menu);
  } catch (e) {}
});
app.on('window-all-closed', () => { stopSupervisor(); app.quit(); });
app.on('before-quit', stopSupervisor);
