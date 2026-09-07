/// <reference types="vite/client" />

interface ElectronApi {
  selectModel: () => Promise<string | null>
  selectDirectory: () => Promise<string | null>
  importGeoSequence: () => Promise<{ path: string; name: string; data: unknown } | null>
  exportGeoSequence: (suggestedName: string, data: unknown) => Promise<{ ok: boolean; path?: string }>
  openPath: (path: string) => Promise<{ ok: boolean; error?: string }>
}

interface Window {
  electronAPI?: ElectronApi
}
