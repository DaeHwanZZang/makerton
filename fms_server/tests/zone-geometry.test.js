'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const g=require('../static/zone-geometry.js');
const close=(a,b)=>assert.ok(Math.abs(a-b)<1e-8,`${a} != ${b}`);
const pointClose=(a,b)=>{close(a.x,b.x);close(a.y,b.y);};

test('회전 사각형은 저장 꼭짓점에서 중심·크기·각도를 복원한다',()=>{
  const r={x:2,y:3,width:4,height:2,rotation:37},pts=g.corners(r),restored=g.fromPolygon(pts);
  pointClose(g.center(restored),{x:4,y:4});close(restored.width,4);close(restored.height,2);close(restored.rotation,37);
  g.corners(restored).forEach((p,i)=>pointClose(p,pts[i]));
  assert.ok(g.fromPolygon([...pts,pts[0]]));
  assert.equal(g.fromPolygon([{x:0,y:0},{x:3,y:0},{x:2,y:2},{x:0,y:2}]),null);
});
test('회전한 모서리 드래그는 반대쪽 꼭짓점을 고정한다',()=>{
  const r={x:2,y:3,width:4,height:2,rotation:45},anchor=g.corners(r)[0];
  const resized=g.resize(r,'se',g.world({x:4,y:3},r),.1);
  pointClose(g.corners(resized)[0],anchor);close(resized.width,6);close(resized.height,4);close(resized.rotation,45);
});
test('회전된 변을 지나 드래그해도 최소 크기를 유지한다',()=>{
  const r={x:2,y:3,width:4,height:2,rotation:-60},anchor=g.corners(r)[1];
  const resized=g.resize(r,'w',g.world({x:10,y:0},r),.2);
  pointClose(g.corners(resized)[1],anchor);close(resized.width,.2);close(resized.height,2);
});
test('이동·범위 검사는 회전한 꼭짓점 전체를 사용한다',()=>{
  const bounds={left:0,top:0,right:10,bottom:10},r={x:2,y:3,width:4,height:2,rotation:45};
  const moved=g.move(r,-100,100,bounds);
  assert.ok(g.fits(moved,bounds));close(moved.width,4);close(moved.height,2);close(moved.rotation,45);
  const e=g.extent(moved);close(e.left,0);close(e.bottom,10);
  assert.equal(g.fits({x:0,y:0,width:4,height:2,rotation:45},bounds),false);
});
