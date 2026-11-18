const test = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const {pageTypes, parseGuide, findScenes, formatSelection} = require('../assets/ui-scenes.js');
const {makeReceipt, compareReceipt, restore} = require('../assets/ui-scenes-demo.js');

const source = readFileSync(join(__dirname, '../docs/guides/统一UI场景规范.md'), 'utf8');
const scenes = parseGuide(source);
const review = pageTypes.find(page => page.id === 'review');

test('every guide scene is reachable from a page type and has all four facts', () => {
  assert.equal(scenes.size, 64);
  assert.equal(pageTypes.length, 10);
  const allMapped = new Set();
  for (const page of pageTypes) {
    assert.equal(new Set(page.scenes).size, page.scenes.length);
    for (const id of page.scenes) allMapped.add(id);
  }
  assert.deepEqual(allMapped, new Set(scenes.keys()));
  assert.equal(findScenes(scenes, review, '')[0].id, 'E10');
  for (const scene of scenes.values()) {
    assert.equal(scene.facts.length, 4);
    assert.ok(scene.facts.every(Boolean));
  }
});

test('filtering matches ID, Chinese terms and component names within the selected page', () => {
  assert.deepEqual(findScenes(scenes, review, 'ｅ１０').map(scene => scene.id), ['E10']);
  assert.deepEqual(findScenes(scenes, review, '复核 e10').map(scene => scene.id), ['E10']);
  assert.deepEqual(findScenes(scenes, review, 'f01'), []);
  assert.ok(findScenes(scenes, null, 'CreateDetailsDialog').some(scene => scene.id === 'A09'));
});

test('the parser reports format drift, missing mapped rows and duplicate rows', () => {
  const row = source.split('\n').find(line => line.startsWith('| **E10**'));
  assert.throws(() => parseGuide(source.replace(row, row.replace('流程配方', '未分类'))), /格式无效/);
  assert.throws(() => parseGuide(source.replace(row, '')), /缺少/);
  assert.throws(() => parseGuide(source.replace(row, row + '\n' + row)), /格式无效/);
});

test('copy selection includes all facts, strips inline markdown, and preserves code names', () => {
  const copy = formatSelection(scenes.get('A09'));
  assert.match(copy, /^A09 /);
  for (const heading of ['可复用组件与实现起点', '平台默认行为', 'Pack 需补的内容', '定向验收']) {
    assert.ok(copy.includes(heading));
  }
  assert.match(copy, /createDetailsDialog/);
  assert.doesNotMatch(copy, /`/);
});

test('demo receipt comparison separates a match, quantity differences, and wrong batch', () => {
  const batch = {id: 'RK-101', quantity: 120};
  assert.equal(compareReceipt(batch, makeReceipt(batch, 'match')), 0);
  assert.equal(compareReceipt(batch, makeReceipt(batch, 'difference')), -1);
  assert.throws(() => compareReceipt(batch, makeReceipt(batch, 'wrong')), /当前批次不符/);
  for (const invalid of [null, {batchId: batch.id, version: 2, quantity: 120},
    {batchId: batch.id, version: 1, quantity: '120'},
    {batchId: batch.id, version: 1, quantity: 2 ** 53}]) {
    assert.throws(() => compareReceipt(batch, invalid), /格式无效/);
  }
});

test('demo restoration retains only known batches and safe synthetic receipt metadata', () => {
  const id = 'REV-9fd70e8e-96c0-4bc4-9c6c-cc6357dd55a2';
  const state = restore({getItem: () => JSON.stringify({selected: 'RK-101', records: {
    'RK-101': {id, confirmed: false, localFile: 'discard'},
    'RK-102': {id: '', confirmed: false},
    'RK-999': {id, confirmed: true},
  }})});
  assert.equal(state.selected.id, 'RK-101');
  assert.deepEqual([...state.records], [['RK-101', {id, confirmed: false}]]);
  assert.equal(restore({getItem() {throw Error('storage denied');}}).selected, null);
});
