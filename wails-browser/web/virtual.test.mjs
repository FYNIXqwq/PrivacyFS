import {test} from 'node:test';
import assert from 'node:assert/strict';
import {geometry} from './virtual.js';
test('millions of rows map to bounded CSS height and reach final row',()=>{
 const g=geometry(12_000_000,360,36,Infinity);
 assert.equal(g.extent,8_000_000);
 assert.equal(g.position,11_999_990);
 assert.equal(g.scroll,g.extent-360);
 assert.equal(g.position+g.visible,12_000_000);
});
test('partial visible rows still align the last row with the viewport bottom',()=>{
 const g=geometry(1000,317,36,Infinity);
 assert.ok(Math.abs((1000-g.position)*36-317)<1e-6);
});
test('empty and small lists do not create an artificial scrollbar',()=>{
 for(const total of [0,1,5]){const g=geometry(total,360,36,100);assert.equal(g.position,0);assert.equal(g.extent,360)}
});
