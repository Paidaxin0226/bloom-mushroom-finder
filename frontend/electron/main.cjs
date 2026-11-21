const { app, BrowserWindow, dialog, ipcMain, Menu, session, shell } = require('electron')
const { spawn } = require('child_process')
const fs = require('fs')
const http = require('http')
const net = require('net')
const path = require('path')

// Reuse the existing desktop profile so Chromium preferences survive the rename.
const appData = app.getPath('appData')
const previousPackagedProfile = path.join(appData, 'BloomPilot')
const previousDesktopProfile = path.join(appData, 'bloompilot-desktop')
const previousProfile = app.isPackaged && fs.existsSync(previousPackagedProfile)
  ? previousPackagedProfile
  : previousDesktopProfile
if (fs.existsSync(previousProfile)) {
  app.setPath('userData', previousProfile)
}

const singleInstance = app.requestSingleInstanceLock()
if (!singleInstance) app.exit(0)

let mainWindow = null
let backendProcess = null
let backendPort = 18777
let allowQuit = false
const MAX_GEO_SEQUENCE_FILE_BYTES = 32 * 1024 * 1024

function projectRoot() {
  return path.resolve(__dirname, '..', '..')
}

function allocatePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer()
    server.once('error', reject)
    server.once('listening', () => {
      const address = server.address()
      server.close(() => resolve(address && typeof address === 'object' ? address.port : 18777))
    })
    server.listen(0, '127.0.0.1')
  })
}

function backendCommand(port) {
  if (app.isPackaged) {
    const executable = path.join(process.resourcesPath, 'backend', 'bloom-mushroom-finder-backend.exe')
    return { executable, args: ['--host', '127.0.0.1', '--port', String(port)], cwd: path.dirname(executable) }
  }
  const root = projectRoot()
  const executable = process.env.BLOOMPILOT_PYTHON || path.join(root, '.venv', 'Scripts', 'python.exe')
  return {
    executable,
    args: ['-m', 'bloompilot.web.server', '--host', '127.0.0.1', '--port', String(port)],
    cwd: root,
  }
}

function startBackend(port) {
  const command = backendCommand(port)
  if (!fs.existsSync(command.executable)) {
    dialog.showErrorBox('Bloom Mushroom Finder backend missing', `找不到 Python backend：\n${command.executable}`)
    app.quit()
    return
  }
  backendProcess = spawn(command.executable, command.args, {
    cwd: command.cwd,
    windowsHide: true,
    stdio: ['ignore', 'pipe', 'pipe'],
    env: { ...process.env, BLOOMPILOT_DESKTOP: '1' },
  })
  backendProcess.stdout.on('data', (data) => process.stdout.write(`[backend] ${data}`))
  backendProcess.stderr.on('data', (data) => process.stderr.write(`[backend] ${data}`))
  backendProcess.on('exit', (code) => {
    console.log(`[backend] exited (${code})`)
    backendProcess = null
    if (!allowQuit && mainWindow) mainWindow.webContents.send('backend-exited', code)
  })
}

function requestBackendStop() {
  return new Promise((resolve) => {
    const request = http.request({
      host: '127.0.0.1',
      port: backendPort,
      path: '/api/system/shutdown',
      method: 'POST',
      timeout: 1800,
    }, (response) => {
      response.resume()
      response.on('end', resolve)
    })
    request.on('error', resolve)
    request.on('timeout', () => { request.destroy(); resolve() })
    request.end()
  })
}

async function stopBackend() {
  const processRef = backendProcess
  if (!processRef) return
  backendProcess = null
  await requestBackendStop()
  if (processRef.exitCode !== null) return
  await new Promise((resolve) => {
    const timer = setTimeout(resolve, 3500)
    processRef.once('exit', () => { clearTimeout(timer); resolve() })
  })
  if (processRef.exitCode === null) try { processRef.kill() } catch { /* already gone */ }
}

function registerIpc() {
  ipcMain.handle('select-model', async () => {
    const result = await dialog.showOpenDialog(mainWindow, {
      properties: ['openFile'],
      filters: [{ name: 'YOLO model', extensions: ['pt'] }],
    })
    return result.canceled ? null : result.filePaths[0]
  })
  ipcMain.handle('select-directory', async () => {
    const result = await dialog.showOpenDialog(mainWindow, { properties: ['openDirectory'] })
    return result.canceled ? null : result.filePaths[0]
  })
  ipcMain.handle('import-geo-sequence', async () => {
    const result = await dialog.showOpenDialog(mainWindow, {
      properties: ['openFile'],
      filters: [{ name: 'Bloom Mushroom Finder 座標序列', extensions: ['json'] }],
    })
    if (result.canceled) return null
    const selectedPath = result.filePaths[0]
    const stats = await fs.promises.stat(selectedPath)
    if (stats.size > MAX_GEO_SEQUENCE_FILE_BYTES) throw new Error('座標序列檔案不能超過 32 MB。')
    const text = await fs.promises.readFile(selectedPath, 'utf8')
    return { path: selectedPath, name: path.basename(selectedPath), data: JSON.parse(text) }
  })
  ipcMain.handle('export-geo-sequence', async (_event, value) => {
    const suggestedName = typeof value?.suggestedName === 'string' ? value.suggestedName : 'geo-sequence'
    const safeName = suggestedName.replace(/[^\p{L}\p{N}_.-]+/gu, '_').slice(0, 80) || 'geo-sequence'
    const text = JSON.stringify(value?.data, null, 2)
    if (Buffer.byteLength(text, 'utf8') > MAX_GEO_SEQUENCE_FILE_BYTES) throw new Error('座標序列資料不能超過 32 MB。')
    const result = await dialog.showSaveDialog(mainWindow, {
      defaultPath: `${safeName.replace(/\.json$/i, '')}.json`,
      filters: [{ name: 'Bloom Mushroom Finder 座標序列', extensions: ['json'] }],
    })
    if (result.canceled || !result.filePath) return { ok: false }
    await fs.promises.writeFile(result.filePath, text + '\n', 'utf8')
    return { ok: true, path: result.filePath }
  })
  ipcMain.handle('open-path', async (_event, target) => {
    if (typeof target !== 'string' || !target.trim()) return { ok: false, error: 'invalid path' }
    const error = await shell.openPath(path.resolve(target))
    return error ? { ok: false, error } : { ok: true }
  })
}

async function createWindow() {
  backendPort = await allocatePort()
  startBackend(backendPort)

  try {
    const osmHosts = new Set([
      'tile.openstreetmap.org',
      'a.tile.openstreetmap.org',
      'b.tile.openstreetmap.org',
      'c.tile.openstreetmap.org',
    ])
    session.defaultSession.webRequest.onBeforeSendHeaders((details, callback) => {
      try {
        if (osmHosts.has(new URL(details.url).hostname)) {
          details.requestHeaders['User-Agent'] = `BloomMushroomFinder/${app.getVersion()}`
        }
      } catch { /* ignore non-URL */ }
      callback({ requestHeaders: details.requestHeaders })
    })
  } catch (error) { console.error('[electron] OSM request hook failed', error) }

  mainWindow = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 1024,
    minHeight: 680,
    title: 'Bloom Mushroom Finder',
    backgroundColor: '#0a0c12',
    show: false,
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      preload: path.join(__dirname, 'preload.cjs'),
    },
  })
  mainWindow.once('ready-to-show', () => mainWindow?.show())
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith('https://') || url.startsWith('http://')) shell.openExternal(url)
    return { action: 'deny' }
  })

  const dev = process.argv.includes('--dev')
  if (dev) {
    await mainWindow.loadURL(`http://127.0.0.1:5173/?apiPort=${backendPort}`)
  } else {
    await mainWindow.loadFile(path.join(__dirname, '..', 'dist', 'index.html'), {
      query: { apiPort: String(backendPort) },
    })
  }
}

Menu.setApplicationMenu(null)
app.on('second-instance', () => {
  if (!mainWindow) return
  if (mainWindow.isMinimized()) mainWindow.restore()
  mainWindow.show()
  mainWindow.focus()
})
app.whenReady().then(() => {
  registerIpc()
  return createWindow()
})
app.on('window-all-closed', () => { if (process.platform !== 'darwin') app.quit() })
app.on('before-quit', (event) => {
  if (allowQuit || !backendProcess) return
  event.preventDefault()
  stopBackend().finally(() => {
    allowQuit = true
    app.quit()
  })
})
app.on('activate', () => { if (BrowserWindow.getAllWindows().length === 0) void createWindow() })
