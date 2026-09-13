import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import { backgroundPage, backgroundTown, filterBackgrounds } from '../src/utils/monthlyBackgrounds.ts'

const backgrounds = Array.from({ length: 73 }, (_, i) => ({ path: `route/wp_00001_Berlin/grid_${i}.png`, name: `grid_${i}.png`, route: 'route', modified_ns: i, size_bytes: 100 }))
test('large collections are paged and deletion clamps the last page', () => {
  assert.equal(backgroundPage(backgrounds, 0, 24).items.length, 24)
  assert.equal(backgroundPage(backgrounds, 3, 24).items.length, 1)
  assert.deepEqual(backgroundPage(backgrounds.slice(0, 72), 3, 24), {page: 2, pages: 3, items: backgrounds.slice(48, 72)})
  assert.deepEqual(backgroundPage([], 5, 24), {page: 0, pages: 1, items: []})
})
test('town names and filenames identify images and filters combine', () => {
  assert.equal(backgroundTown(backgrounds[0]), 'Berlin')
  const items = [...backgrounds, { ...backgrounds[0], path: 'route/wp_00002_New_York/special.png' }]
  assert.equal(filterBackgrounds(items, 'SPECIAL', 'New York', 'name').length, 1)
  assert.equal(filterBackgrounds(items, 'special', 'Berlin', 'name').length, 0)
  assert.equal(filterBackgrounds(items, '  new YORK ', '', 'name').length, 1)
})
test('sorting does not mutate server data and newest frames appear first', () => {
  assert.equal(filterBackgrounds(backgrounds, '', '', 'newest')[0].modified_ns, 72)
  assert.equal(filterBackgrounds(backgrounds, '', '', 'oldest')[0].modified_ns, 0)
  assert.equal(backgrounds[0].modified_ns, 0)
})
test('desktop CSP permits local backend images with dynamically allocated ports', () => {
  const html = readFileSync(new URL('../index.html', import.meta.url), 'utf8')
  assert.match(html.match(/img-src[^;]+/)[0], /http:\/\/127\.0\.0\.1:\*/)
})
