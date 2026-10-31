'use strict';
// x/y는 회전 전 좌상단, rotation은 중심 기준 시계 방향 도 단위다 (world y는 아래로 증가).
(() => {
  const angle = r => (r.rotation || 0) * Math.PI / 180;
  const center = r => ({x:r.x+r.width/2,y:r.y+r.height/2});
  function world(p,r) {
    const c=center(r), a=angle(r), co=Math.cos(a), si=Math.sin(a);
    return {x:c.x+co*p.x-si*p.y,y:c.y+si*p.x+co*p.y};
  }
  function local(p,r) {
    const c=center(r),a=angle(r),dx=p.x-c.x,dy=p.y-c.y;
    return {x:Math.cos(a)*dx+Math.sin(a)*dy,y:-Math.sin(a)*dx+Math.cos(a)*dy};
  }
  function corners(r) {
    return [[-1,-1],[1,-1],[1,1],[-1,1]].map(([x,y])=>world({x:x*r.width/2,y:y*r.height/2},r));
  }
  function fromPolygon(polygon) {
    const pts=[...polygon];
    if(pts.length===5&&Math.hypot(pts[0].x-pts[4].x,pts[0].y-pts[4].y)<1e-8)pts.pop();
    if(pts.length!==4||pts.some(p=>!Number.isFinite(p.x)||!Number.isFinite(p.y)))return null;
    const [a,b,c,d]=pts,ux=b.x-a.x,uy=b.y-a.y,vx=d.x-a.x,vy=d.y-a.y;
    const width=Math.hypot(ux,uy),height=Math.hypot(vx,vy);
    if(Math.min(width,height)<=1e-9||Math.abs(ux*vx+uy*vy)>1e-6*width*height||
      Math.hypot(c.x-(b.x+d.x-a.x),c.y-(b.y+d.y-a.y))>1e-6*Math.max(width,height))return null;
    return {x:(a.x+c.x-width)/2,y:(a.y+c.y-height)/2,width,height,rotation:Math.atan2(uy,ux)*180/Math.PI};
  }
  function extent(r) {
    const pts=corners(r);
    return {left:Math.min(...pts.map(p=>p.x)),right:Math.max(...pts.map(p=>p.x)),
      top:Math.min(...pts.map(p=>p.y)),bottom:Math.max(...pts.map(p=>p.y))};
  }
  function fits(r,b) {
    const e=extent(r),eps=1e-9;
    return e.left>=b.left-eps&&e.right<=b.right+eps&&e.top>=b.top-eps&&e.bottom<=b.bottom+eps;
  }
  function move(r,dx,dy,b) {
    const candidate={...r,x:r.x+dx,y:r.y+dy},e=extent(candidate);
    if(e.right-e.left>b.right-b.left+1e-9||e.bottom-e.top>b.bottom-b.top+1e-9)return r;
    candidate.x+=Math.max(b.left-e.left,Math.min(0,b.right-e.right));
    candidate.y+=Math.max(b.top-e.top,Math.min(0,b.bottom-e.bottom));
    return candidate;
  }
  function resize(r,handle,p,minSize) {
    const q=local(p,r);
    let left=-r.width/2,right=r.width/2,top=-r.height/2,bottom=r.height/2;
    if(handle.includes('w'))left=Math.min(q.x,right-minSize);
    if(handle.includes('e'))right=Math.max(q.x,left+minSize);
    if(handle.includes('n'))top=Math.min(q.y,bottom-minSize);
    if(handle.includes('s'))bottom=Math.max(q.y,top+minSize);
    const c=world({x:(left+right)/2,y:(top+bottom)/2},r),width=right-left,height=bottom-top;
    return {...r,x:c.x-width/2,y:c.y-height/2,width,height};
  }
  const geometry={center,world,local,corners,fromPolygon,extent,fits,move,resize};
  if(typeof module!=='undefined'&&module.exports)module.exports=geometry;
  else window.zoneGeometry=geometry;
})();
