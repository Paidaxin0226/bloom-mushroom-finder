import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { sanitizeTerminalOutput } from '../src/utils/terminalOutput.ts'

const cases = JSON.parse(readFileSync(new URL('../../tests/fixtures/terminal_output.json', import.meta.url), 'utf8'))
for (const { name, raw, expected } of cases) {
  test(name, () => assert.equal(sanitizeTerminalOutput(raw), expected))
}
