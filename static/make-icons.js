// مولد بدون وابستگی برای آیکن‌های باینری PWA (PNG استاندارد RGBA).
const fs=require('fs'),zlib=require('zlib');
const table=(()=>{let t=[];for(let n=0;n<256;n++){let c=n;for(let k=0;k<8;k++)c=(c&1)?0xedb88320^(c>>>1):c>>>1;t[n]=c>>>0}return t})();
function crc(buf){let c=0xffffffff;for(const b of buf)c=table[(c^b)&255]^(c>>>8);return(c^0xffffffff)>>>0}
function chunk(type,data){let t=Buffer.from(type),len=Buffer.alloc(4),sum=Buffer.alloc(4);len.writeUInt32BE(data.length);sum.writeUInt32BE(crc(Buffer.concat([t,data])));return Buffer.concat([len,t,data,sum])}
function insideRound(x,y,x0,y0,x1,y1,r){let cx=Math.max(x0+r,Math.min(x,x1-r)),cy=Math.max(y0+r,Math.min(y,y1-r));return(x-cx)**2+(y-cy)**2<=r*r}
function make(size,path){let raw=Buffer.alloc((size*4+1)*size),p=0;for(let y=0;y<size;y++){raw[p++]=0;for(let x=0;x<size;x++){let r=8,g=21,b=38,a=255;let pad=size*.19,x0=pad,y0=size*.17,x1=size-pad,y1=size*.69,rad=size*.085;let tail=x>size*.27&&x<size*.48&&y>=y1&&y<size*.83&&x<(size*.48-(y-y1)*.62);if(insideRound(x,y,x0,y0,x1,y1,rad)||tail){let q=(x+y)/(size*2);r=70+Math.round(15*q);g=208-Math.round(42*q);b=166+Math.round(86*q);let line1=y>size*.35&&y<size*.40&&x>size*.34&&x<size*.68,line2=y>size*.48&&y<size*.53&&x>size*.34&&x<size*.58;if(line1||line2){r=7;g=24;b=35}}raw[p++]=r;raw[p++]=g;raw[p++]=b;raw[p++]=a}}
  let sig=Buffer.from([137,80,78,71,13,10,26,10]),ih=Buffer.alloc(13);ih.writeUInt32BE(size,0);ih.writeUInt32BE(size,4);ih[8]=8;ih[9]=6;fs.writeFileSync(path,Buffer.concat([sig,chunk('IHDR',ih),chunk('IDAT',zlib.deflateSync(raw,{level:9})),chunk('IEND',Buffer.alloc(0))]))}
make(192,__dirname+'/icon-192.png');make(512,__dirname+'/icon-512.png');
