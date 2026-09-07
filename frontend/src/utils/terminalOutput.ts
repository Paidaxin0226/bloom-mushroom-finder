// The desktop log is plain text, not a terminal. Filter at render time too so
// messages from an older backend cannot expose progress-bar control sequences.
const ansiEscape = /\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]|\x1b[@-_]/g

export function sanitizeTerminalOutput(text: string): string {
  return text
    .replace(ansiEscape, '')
    .replace(/\x00/g, '')
    .replace(/\r\n/g, '\n')
    .split('\n')
    .map((line) => line.slice(line.lastIndexOf('\r') + 1))
    .join('\n')
    .trimEnd()
}
