// Node standard-library worker for pinned Linux images without Python.
// Arguments are JSON data; the worker source is supplied only by the trusted host.
"use strict";
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const {TextDecoder} = require("node:util");
const C = fs.constants;
const SKIP = new Set([".git", ".minidsh", ".venv", "node_modules", "dist", "build"]);
const SENSITIVE = new Set([".env", ".npmrc", ".pypirc", "credentials", "credentials.json", "credentials.toml", "id_rsa", "id_ed25519"]);
const FILE_BYTES = 256000, SKILL_BYTES = 32768, SKILL_COUNT = 32;
const fail = (code) => { throw new Error(code); };
const decode = (raw, code) => { try { return new TextDecoder("utf-8", {fatal:true,ignoreBOM:true}).decode(raw); } catch { fail(code); } };
const compare = (a,b) => Buffer.compare(Buffer.from(a), Buffer.from(b));
let root;
function inside(value) {
  const relative = path.relative(root, value);
  return relative === "" || (relative !== ".." && !relative.startsWith("../") && !path.isAbsolute(relative));
}
function resolve(value) {
  if (typeof value !== "string" || value.includes("\0") || path.isAbsolute(value)) fail("file_backend_denied");
  let candidate = path.resolve(root, value), missing = [];
  if (!inside(candidate)) fail("file_backend_denied");
  while (true) {
    try { fs.lstatSync(candidate); break; }
    catch (error) {
      if (error.code !== "ENOENT") throw error;
      missing.unshift(path.basename(candidate));
      candidate = path.dirname(candidate);
    }
  }
  candidate = path.join(fs.realpathSync(candidate), ...missing);
  if (!inside(candidate)) fail("file_backend_denied");
  const parts = path.relative(root, candidate).split(path.sep), lower = parts.map(p=>p.toLowerCase());
  const name = path.basename(candidate).toLowerCase();
  if ((lower.includes(".minidsh") && !parts.includes("artifacts")) || lower.includes(".ssh") ||
      SENSITIVE.has(name) || name.startsWith(".env.") || /\.(key|pem|p12)$/i.test(name)) fail("file_backend_denied");
  return candidate;
}
function directoryFd(parts) {
  let fd = fs.openSync(root, C.O_RDONLY | C.O_DIRECTORY | C.O_NOFOLLOW);
  try {
    for (const part of parts) {
      const next = fs.openSync(`/proc/self/fd/${fd}/${part}`, C.O_RDONLY | C.O_DIRECTORY | C.O_NOFOLLOW);
      fs.closeSync(fd); fd = next;
    }
    return fd;
  } catch (error) { fs.closeSync(fd); throw error; }
}
function readBytes(target, maximum, skill = false) {
  let parent, fd;
  try {
    if (process.platform === "linux") {
      const parts = path.relative(root, target).split(path.sep);
      const name = parts.pop(); parent = directoryFd(parts);
      fd = fs.openSync(`/proc/self/fd/${parent}/${name}`, C.O_RDONLY | C.O_NOFOLLOW | C.O_NONBLOCK);
    } else {
      if (skill) fail("skill_source_denied");
      fd = fs.openSync(target, C.O_RDONLY | C.O_NOFOLLOW | C.O_NONBLOCK);
    }
    const before = fs.fstatSync(fd);
    if (!before.isFile()) fail(skill ? "skill_source_denied" : "file_backend_denied");
    if (before.size > maximum) fail(skill ? "skill_size_limit" : "file_backend_denied");
    const buffer = Buffer.alloc(maximum + 1); let size = 0;
    while (size < buffer.length) { const count = fs.readSync(fd, buffer, size, buffer.length-size, null); if (!count) break; size += count; }
    const after = fs.fstatSync(fd);
    if (size > maximum) fail(skill ? "skill_size_limit" : "file_backend_denied");
    if (before.size !== after.size || before.mtimeMs !== after.mtimeMs || before.ctimeMs !== after.ctimeMs) fail(skill ? "skill_changed" : "file_backend_denied");
    return buffer.subarray(0,size);
  } finally { if (fd !== undefined) fs.closeSync(fd); if (parent !== undefined) fs.closeSync(parent); }
}
function read(target) { return decode(readBytes(target, FILE_BYTES), "file_backend_denied"); }
function writable(target) {
  if (path.relative(root,target).split(path.sep).some(p=>p.toLowerCase()===".minidsh")) fail("file_backend_denied");
  const parent = fs.realpathSync(path.dirname(target));
  if (!inside(parent)) fail("file_backend_denied");
  return parent;
}
function write(target, content) {
  if (typeof content !== "string" || !content.isWellFormed()) fail("file_backend_denied");
  const parent = writable(target); let parentFd;
  if (process.platform === "linux") parentFd = directoryFd(path.relative(root,parent).split(path.sep).filter(Boolean));
  const base = parentFd === undefined ? parent : `/proc/self/fd/${parentFd}`;
  const temporary = path.join(base,".minidsh-node-"+crypto.randomBytes(16).toString("hex"));
  let fd;
  try {
    fd = fs.openSync(temporary,C.O_WRONLY|C.O_CREAT|C.O_EXCL|C.O_NOFOLLOW,0o600);
    fs.writeFileSync(fd,content,{encoding:"utf8"}); fs.fsyncSync(fd); fs.closeSync(fd); fd=undefined;
    fs.renameSync(temporary,path.join(base,path.basename(target)));
  } finally {
    if (fd !== undefined) fs.closeSync(fd);
    try { fs.unlinkSync(temporary); } catch(error) { if (error.code !== "ENOENT") throw error; }
    if (parentFd !== undefined) fs.closeSync(parentFd);
  }
}
function updated(original,args) {
  const expected=args.expected, replacement=args.replacement;
  if (typeof expected!=="string" || !expected || typeof replacement!=="string") fail("patch_mismatch");
  const positions=[]; let offset=0;
  while ((offset=original.indexOf(expected,offset)) !== -1) {positions.push(offset); offset+=expected.length;}
  if (!positions.length) fail("patch_mismatch");
  if (positions.length>1 && args.occurrence==null) fail("patch_ambiguous");
  const target=args.occurrence ?? 1;
  if (!Number.isInteger(target)||target<1||target>positions.length) fail("patch_mismatch");
  const index=positions[target-1];return original.slice(0,index)+replacement+original.slice(index+expected.length);
}
function lines(text) { if (!text) return []; const result=text.split(/\r\n|[\n\r\v\f\u0085\u2028\u2029]/); if(result.at(-1)==="") result.pop(); return result; }
function preview(name,oldValue,newValue) {
  if(oldValue===newValue) return "";
  const before=lines(oldValue), after=lines(newValue);
  return `--- ${name}\n+++ ${name}\n@@ -${before.length?1:0},${before.length} +${after.length?1:0},${after.length} @@\n` +
    before.map(v=>"-"+v+"\n").join("")+after.map(v=>"+"+v+"\n").join("");
}
function skillPath(value) {
  if(typeof value!=="string"||!value||path.isAbsolute(value)||value.split("/").includes("..")) fail("skill_path_invalid");
  try {
    const target=resolve(value), parts=path.relative(root,target).split(path.sep).map(v=>v.toLowerCase());
    if(parts.some(p=>SENSITIVE.has(p)||p===".ssh"||p===".minidsh"||p.startsWith(".env."))) fail("skill_source_denied");
    return target;
  } catch { fail("skill_source_denied"); }
}
function skillDocument(target, expected) {
  if(path.basename(target)!=="SKILL.md") fail("skill_source_denied");
  const raw=readBytes(target,SKILL_BYTES,true), sha256=crypto.createHash("sha256").update(raw).digest("hex");
  if(expected!==undefined && expected!==sha256) fail("skill_changed");
  return {path:path.relative(root,target).split(path.sep).join("/"),sha256,content:decode(raw,"skill_encoding_invalid")};
}
function skillScan(roots) {
  const visited=new Set(), documents=new Set();
  function walk(directory) {
    directory=skillPath(path.relative(root,directory)||".");
    if(visited.has(directory)) fail("skill_source_cycle"); visited.add(directory);
    for(const name of fs.readdirSync(directory).sort(compare)) {
      const item=path.join(directory,name), info=fs.lstatSync(item);
      if(info.isSymbolicLink()) skillPath(path.relative(root,item));
      if(SKIP.has(name)) continue;
      if(fs.statSync(item).isDirectory()) walk(item);
      else if(name==="SKILL.md") {
        const target=skillPath(path.relative(root,item));
        if(documents.has(target)) fail("skill_duplicate_source"); documents.add(target);
        if(documents.size>SKILL_COUNT) fail("skill_count_limit");
      }
    }
  }
  for(const value of [...roots].sort(compare)) {const directory=skillPath(value); if(!fs.existsSync(directory)) fail("skill_root_missing");walk(directory);}
  return {documents:[...documents].sort(compare).map(v=>skillDocument(v))};
}
function matches(name,pattern) {
  // Bounded dynamic-programming wildcard matching avoids regex backtracking.
  if(pattern.length>512) fail("file_backend_denied");
  const chars=Array.from(name), tokens=[];
  for(let i=0;i<pattern.length;i++) {
    const symbol=pattern[i];
    if(symbol==="[") {
      let start=i+1, end=start;const negate=pattern[start]==="!";if(negate) end++;
      if(pattern[end]==="]") end++;
      while(end<pattern.length&&pattern[end]!=="]") end++;
      if(end<pattern.length) {
        const members=Array.from(pattern.slice(start+(negate?1:0),end));
        tokens.push({test:(value)=>{let found=false;for(let j=0;j<members.length;j++) {if(j+2<members.length&&members[j+1]==="-") {found ||= value.codePointAt(0)>=members[j].codePointAt(0)&&value.codePointAt(0)<=members[j+2].codePointAt(0);j+=2;}else found ||= value===members[j];}return negate?!found:found;}});
        i=end;continue;
      }
    }
    tokens.push(symbol==="*"?{star:true}:{test:(value)=>symbol==="?"||value===symbol});
  }
  let previous=Array(chars.length+1).fill(false);previous[0]=true;
  for(const token of tokens) {const next=Array(chars.length+1).fill(false);next[0]=!!token.star&&previous[0];for(let i=1;i<=chars.length;i++) next[i]=token.star?(previous[i]||next[i-1]):(previous[i-1]&&token.test(chars[i-1]));previous=next;}
  return previous[chars.length];
}
function dispatch(operation,args) {
  if(operation==="skill_scan") {
    if(Object.keys(args).join()!=="roots"||!Array.isArray(args.roots)||args.roots.some(v=>typeof v!=="string")) fail("skill_roots_invalid");
    return skillScan(args.roots);
  }
  if(operation==="skill_read") {
    if(Object.keys(args).sort().join()!=="expected_sha256,path"||!/^[a-f0-9]{64}$/.test(args.expected_sha256)) fail("skill_read_invalid");
    return skillDocument(skillPath(args.path),args.expected_sha256);
  }
  if(operation==="prepare") {
    const target=resolve(args.arguments.path); writable(target);
    const oldValue=fs.existsSync(target)?read(target):""; let value;
    if(args.tool==="patch_file") value=updated(oldValue,args.arguments);
    else if(args.tool==="write_file") {if(fs.existsSync(target)&&!args.arguments.overwrite) fail("file_exists");value=args.arguments.content;}
    else fail("file_backend_denied");
    return {scope:target,preview:preview(args.arguments.path,oldValue,value)};
  }
  const target=resolve(args.path??".");
  if(operation==="read_file") {
    const contentLines=lines(read(target)), start=args.start_line??1, end=args.end_line||contentLines.length;
    if(end<start) fail("invalid_range");
    const content=contentLines.slice(start-1,end).join("\n"), characters=Array.from(content), maximum=args.max_chars??20000;
    return {path:args.path,start_line:start,end_line:Math.min(end,contentLines.length),content:characters.slice(0,maximum).join(""),truncated:characters.length>maximum};
  }
  if(operation==="write_file"||operation==="patch_file") {
    if(operation==="write_file"&&fs.existsSync(target)&&!args.overwrite) fail("file_exists");
    const content=operation==="write_file"?args.content:updated(read(target),args);write(target,content);
    const result={path:args.path,bytes_written:Buffer.byteLength(content)};if(operation==="patch_file") result.occurrence=args.occurrence??1;return result;
  }
  if(!["list_files","search","search_code"].includes(operation)) fail("file_backend_denied");
  const candidates=[];
  function walk(directory) {for(const name of fs.readdirSync(directory).sort(compare)) {if(SKIP.has(name)) continue;const item=path.join(directory,name);const info=fs.lstatSync(item);if(info.isDirectory()) walk(item);else candidates.push(item);}}
  if(fs.statSync(target).isFile()) candidates.push(target);else walk(target);
  const output=[], maximum=args.max_results??args.max_files??100;let truncated=false;
  for(const candidate of candidates.sort(compare)) {
    if(!matches(path.basename(candidate),args.glob??"*")) continue;
    let values=[];try {
      const actual=resolve(path.relative(root,candidate)), relative=path.relative(root,candidate).split(path.sep).join("/");
      if(!fs.statSync(actual).isFile()) continue;
      if(operation==="list_files") values=[path.relative(root,actual).split(path.sep).join("/")];
      else {const content=read(actual);if(operation==="search") values=lines(content).flatMap((text,i)=>text.includes(args.query)?[{path:relative,line:i+1,text:Array.from(text).slice(0,500).join("")}]:[]);else {const count=args.query?content.split(args.query).length-1:content.length+1;values=count?[{path:relative,matches:count}]:[];}}
    }catch{continue;}
    for(const value of values) {if(output.length>=maximum) {truncated=true;break;}output.push(value);}if(truncated) break;
  }
  const key={list_files:"files",search:"matches",search_code:"candidates"}[operation];
  if(operation==="search_code") output.sort((a,b)=>b.matches-a.matches||compare(a.path,b.path));
  return {[key]:output,truncated};
}
let payload;
try {const request=JSON.parse(process.argv[1]);root=fs.realpathSync(process.argv[2]||"/workspace");payload={result:dispatch(request.operation,request.arguments),error:null};}
catch(error) {const code=String(error.message);payload={result:null,error:/^(skill_[a-z_]+|patch_mismatch|patch_ambiguous|file_exists|invalid_range)$/.test(code)?code:"file_backend_denied"};}
process.stdout.write(JSON.stringify(payload));
