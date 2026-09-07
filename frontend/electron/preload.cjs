const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('electronAPI', {
  selectModel: () => ipcRenderer.invoke('select-model'),
  selectDirectory: () => ipcRenderer.invoke('select-directory'),
  importGeoSequence: () => ipcRenderer.invoke('import-geo-sequence'),
  exportGeoSequence: (suggestedName, data) => ipcRenderer.invoke('export-geo-sequence', { suggestedName, data }),
  openPath: (target) => ipcRenderer.invoke('open-path', target),
})
