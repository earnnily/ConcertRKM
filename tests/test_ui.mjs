import fs from 'node:fs';import vm from 'node:vm';import assert from 'node:assert/strict';import {webcrypto} from 'node:crypto';
const nodes=new Map();function node(){return {value:'',innerHTML:'',textContent:'',children:[],classList:{toggle(){},add(){},remove(){}},appendChild(n){this.children.push(n)},remove(){},scrollTop:0,scrollHeight:0}}
const document={getElementById(id){if(!nodes.has(id))nodes.set(id,node());return nodes.get(id)},querySelectorAll(){return []},createElement:node};
const html=fs.readFileSync(new URL('../dist/index.html',import.meta.url),'utf8');const code=html.match(/<script>([\s\S]*?)<\/script>/)[1];
const context=vm.createContext({document,console,crypto:webcrypto,setInterval(){},setTimeout(){},clearTimeout(){},window:{scrollTo(){}},confirm(){return true},URL,Blob});
vm.runInContext(code,context);
const grid=nodes.get('floorGrid').innerHTML;
assert.equal((grid.match(/floor-cell vip /g)||[]).length,42);assert.equal((grid.match(/floor-cell special /g)||[]).length,14);assert.equal((grid.match(/floor-cell lawn/g)||[]).length,250);assert.equal((grid.match(/<span>\d{4}<\/span>/g)||[]).length,1000);
assert.ok(grid.includes('โต๊ะ A4 ว่าง'));
vm.runInContext("setLawnView('table')",context);assert.equal((nodes.get('floorGrid').innerHTML.match(/4 บัตร \/ โต๊ะ/g)||[]).length,250);
vm.runInContext("draft={zone:'lawn',quantity:3,extras:0,sales_unit:'table',name:'ทดสอบ',phone:'0800000000',tables:[]};createDemo();",context);
assert.equal(vm.runInContext('records[0].quantity',context),12);assert.equal(vm.runInContext('records[0].total',context),2160);assert.ok(nodes.get('lawnSummary').textContent.includes('988'));
vm.runInContext("cancelDemo();draft={zone:'vip',quantity:1,extras:2,name:'ทดสอบ',phone:'0800000000',tables:['A4']};createDemo();",context);
assert.equal(vm.runInContext("tableState('A4')",context),'pending');assert.equal(vm.runInContext('records[0].total',context),2400);
console.log('UI logic passed: 42 VIP, 14 special, 250 lawn tables / 1000 ticket markers; shared quota; A4 selection. Browser layout not tested.');
