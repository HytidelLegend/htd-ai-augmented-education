import JSZip from 'jszip';
import { now, uid, validateProject, type DiagnosticEvent, type DiagnosticEventName, type DiagnosticResult, type Project, type ProjectView, type RecentProject } from './model';

export interface WorkspaceHandle extends FileSystemDirectoryHandle { entries(): AsyncIterableIterator<[string, FileSystemHandle]>; requestPermission?(descriptor?:{mode:'read'|'readwrite'}):Promise<PermissionState>; queryPermission?(descriptor?:{mode:'read'|'readwrite'}):Promise<PermissionState> }
declare global { interface Window { showDirectoryPicker?: (options?:{mode?:'read'|'readwrite'})=>Promise<WorkspaceHandle> } }
const DB='beitu-workspace';const STORE='handles';const LOCK_STORE='locks';
function db():Promise<IDBDatabase>{return new Promise((resolve,reject)=>{const req=indexedDB.open(DB,2);req.onupgradeneeded=()=>{if(!req.result.objectStoreNames.contains(STORE))req.result.createObjectStore(STORE);if(!req.result.objectStoreNames.contains(LOCK_STORE))req.result.createObjectStore(LOCK_STORE)};req.onsuccess=()=>resolve(req.result);req.onerror=()=>reject(req.error)})}
async function handleStore<T>(mode:IDBTransactionMode,fn:(store:IDBObjectStore)=>IDBRequest<T>):Promise<T>{const database=await db();return new Promise((resolve,reject)=>{const tx=database.transaction(STORE,mode);const req=fn(tx.objectStore(STORE));req.onsuccess=()=>resolve(req.result);req.onerror=()=>reject(req.error);tx.oncomplete=()=>database.close();tx.onerror=()=>reject(tx.error)})}
export async function rememberWorkspace(handle:WorkspaceHandle){await handleStore('readwrite',s=>s.put(handle,'workspace'))}
export async function rememberedWorkspace():Promise<WorkspaceHandle|undefined>{return handleStore('readonly',s=>s.get('workspace'))}
export async function chooseWorkspace(){if(!window.showDirectoryPicker)throw Error('此浏览器不支持目录读写，请使用最新版 Chrome 或 Edge');const handle=await window.showDirectoryPicker({mode:'readwrite'});await rememberWorkspace(handle);return handle}
export async function ensurePermission(handle:WorkspaceHandle){if(await handle.queryPermission?.({mode:'readwrite'})==='granted')return true;return await handle.requestPermission?.({mode:'readwrite'})==='granted'}
export async function requireWorkspace(handle:WorkspaceHandle|null):Promise<WorkspaceHandle>{if(!handle)throw Error('请先选择项目目录');if(await handle.queryPermission?.({mode:'readwrite'})!=='granted')throw Error('目录读写权限已失效，请重新选择项目目录');return handle}
const safeName=(v:string)=>v.replace(/[<>:"/\\|?*\x00-\x1f]/g,'_').trim().slice(0,80)||'未命名项目';
const dirBase=(p:Project)=>`${safeName(p.title)}_${p.createdAt.replace(/[-:]/g,'').replace(/\.\d+Z$/,'Z').replace('Z','').replace('T','T').slice(0,15)}`;
const DRAFT_DIR='.__beitu_draft__';
const DIAGNOSTIC_DIR='.__beitu_diagnostics__';
const WORKSPACE_FILE='.__beitu_workspace__.json';
const diagnosticRunId=uid();
export const writerId=uid();
let diagnosticSequence=0;
const ORDER_FILE='project-order.json';
const NATIVE_API_BASE=((import.meta.env.VITE_BEITU_API_URL as string|undefined)??'http://127.0.0.1:5174').replace(/\/$/,'');
interface NativeProjectRecord {project:Project;hash:string;fileSize:number;fileLastModified:number;verifiedHash?:string;operationId?:string}
export interface ProjectOrder { schemaVersion: 1; projectIds: string[] }
function validateProjectOrder(value:unknown):ProjectOrder {
  if(!value || typeof value!=='object') throw Error('项目排序文件无效');
  const order=value as Partial<ProjectOrder>;
  if(order.schemaVersion!==1 || !Array.isArray(order.projectIds) || order.projectIds.some(id=>typeof id!=='string'||!/^[a-f0-9-]{36}$/i.test(id))) throw Error('项目排序文件无效');
  return {schemaVersion:1,projectIds:[...new Set(order.projectIds)]};
}
export async function readProjectOrder(workspace:WorkspaceHandle):Promise<string[]|null>{try{const file=await workspace.getFileHandle(ORDER_FILE);return validateProjectOrder(JSON.parse(await (await file.getFile()).text())).projectIds}catch{return null}}
export async function saveProjectOrder(workspace:WorkspaceHandle,projectIds:string[]){const order:ProjectOrder={schemaVersion:1,projectIds:[...new Set(projectIds)]};await putFile(workspace,ORDER_FILE,JSON.stringify(order,null,2)+'\n')}
async function findDir(workspace:WorkspaceHandle,id:string){for await(const [name,h] of workspace.entries()){if(h.kind!=='directory'||name.startsWith('.'))continue;try{const d=h as FileSystemDirectoryHandle;const f=await d.getFileHandle('project.json');const p=JSON.parse(await (await f.getFile()).text());if(p.projectId===id)return {name,dir:h as FileSystemDirectoryHandle}}catch{}}throw Error('项目不存在')}
const readFile=async(dir:FileSystemDirectoryHandle,path:string)=>{const [a,b]=path.split('/');return (await (await (await dir.getDirectoryHandle(a)).getFileHandle(b)).getFile())};
async function writeFileExclusive(dir:FileSystemDirectoryHandle,name:string,body:Blob|string){
  const h=await dir.getFileHandle(name,{create:true});
  // A siloed writable stream can coexist with another stream for the same
  // file. The last stream to close may then replace a newer save. Make every
  // application write exclusive and abort the stream on failure.
  const createWritable=h.createWritable.bind(h) as unknown as (options:{mode:'exclusive'})=>ReturnType<FileSystemFileHandle['createWritable']>;
  const w=await createWritable({mode:'exclusive'});
  try { await w.write(body); await w.close() }
  catch(error) { await w.abort().catch(()=>undefined); throw error }
}
async function putFile(dir:FileSystemDirectoryHandle,name:string,body:Blob|string){await writeFileExclusive(dir,name,body)}
async function nativeRequest<T>(path:string,init:RequestInit={}):Promise<T>{
  let response:Response;try{response=await fetch(`${NATIVE_API_BASE}${path}`,{...init,headers:{'Content-Type':'application/json',...(init.headers??{})}})}catch{throw Error('本地写入服务未启动，请通过 run.ps1 启动 GUI')}
  const body=await response.json().catch(()=>({error:'本地写入服务返回了无效响应'}));
  if(!response.ok){const message=body.error==='PROJECT_REVISION_CONFLICT'?`项目版本冲突（编辑基于 ${body.expectedRevision}，磁盘为 ${body.actualRevision}），请先保留草稿或重新打开`:body.error;throw Object.assign(Error(typeof message==='string'?message:'本地写入服务请求失败'),{code:body.error,details:body})}return body as T
}
async function nativeReadProjectRecord(id:string):Promise<NativeProjectRecord>{return nativeRequest<NativeProjectRecord>(`/api/projects/${encodeURIComponent(id)}`)}
async function nativeListProjects():Promise<NativeProjectRecord[]>{return nativeRequest<NativeProjectRecord[]>('/api/projects')}
async function nativeSaveProject(directoryName:string,project:Project,operationId:string):Promise<NativeProjectRecord>{return nativeRequest<NativeProjectRecord>('/api/projects/save',{method:'POST',body:JSON.stringify({directoryName,project,operationId})})}
async function sha256(value:string){const bytes=new TextEncoder().encode(value);const digest=await crypto.subtle.digest('SHA-256',bytes);return Array.from(new Uint8Array(digest),b=>b.toString(16).padStart(2,'0')).join('')}
// directoryName is storage metadata, not part of the project business snapshot.
// Excluding it keeps pre-save and readback hashes comparable across create/import/
// draft-recovery flows where the directory is assigned during persistence.
const projectSnapshot=(project:Project)=>{
  const validated=validateProject(project);
  const {directoryName: _directoryName, revision: _revision, lastWriterId: _lastWriterId, ...businessProject}=validated;
  return JSON.stringify(businessProject);
};
export async function projectHash(project:Project){return sha256(projectSnapshot(project))}
async function acquireWorkspaceLock(key:string):Promise<()=>Promise<void>>{
  const lockKey=`workspace:${key}`;const token=uid();const database=await db();
  return new Promise((resolve,reject)=>{const tx=database.transaction(LOCK_STORE,'readwrite');const store=tx.objectStore(LOCK_STORE);const req=store.get(lockKey);req.onsuccess=()=>{const existing=req.result as {createdAt:number}|undefined;if(existing&&Date.now()-existing.createdAt<30000){database.close();reject(Error('项目工作区正在被其他保存操作占用'))}else{store.put({createdAt:Date.now(),writerId,token},lockKey);tx.oncomplete=()=>{database.close();resolve(async()=>{const releaseDb=await db();await new Promise<void>((done,fail)=>{const releaseTx=releaseDb.transaction(LOCK_STORE,'readwrite');const releaseStore=releaseTx.objectStore(LOCK_STORE);const current=releaseStore.get(lockKey);current.onsuccess=()=>{if((current.result as {token?:string}|undefined)?.token===token)releaseStore.delete(lockKey)};releaseTx.oncomplete=()=>{releaseDb.close();done()};releaseTx.onerror=()=>{releaseDb.close();fail(releaseTx.error)}})})}}};req.onerror=()=>{database.close();reject(req.error)}})
}
async function workspaceId(workspace:WorkspaceHandle):Promise<string>{try{const marker=JSON.parse(await workspace.getFileHandle(WORKSPACE_FILE).then(h=>h.getFile()).then(f=>f.text())) as {schemaVersion?:number;workspaceId?:string};if(marker.schemaVersion===1&&typeof marker.workspaceId==='string'&&marker.workspaceId)return marker.workspaceId}catch{}const id=uid();await putFile(workspace,WORKSPACE_FILE,JSON.stringify({schemaVersion:1,workspaceId:id})+'\n');return id}
type PersistencePhase='idle'|'locking'|'locating'|'writing_assets'|'native_committing'|'verifying_final'|'committed'|'failed';
const persistenceTransitions:Record<PersistencePhase,readonly PersistencePhase[]>={idle:['locking'],locking:['locating','failed'],locating:['writing_assets','native_committing','failed'],writing_assets:['native_committing','failed'],native_committing:['verifying_final','failed'],verifying_final:['committed','failed'],committed:[],failed:[]};
function transitionPersistence(current:PersistencePhase,next:PersistencePhase):PersistencePhase{if(!persistenceTransitions[current].includes(next))throw Error(`非法持久化状态迁移: ${current} → ${next}`);return next}
export function conflictDiagnostic(error:unknown){
  const details=(error as {details?:Record<string,unknown>}).details??{};
  const allowed=['expectedRevision','actualRevision','serviceWorkspace','servicePid','requestSource','conflictStage'] as const;
  return Object.fromEntries(allowed.filter(key=>details[key]!==undefined).map(key=>[key,details[key]])) as Partial<DiagnosticEvent>;
}
export async function appendDiagnostic(workspace:WorkspaceHandle,event:DiagnosticEventName,phase:string,project:Project,editVersion:number,result:DiagnosticResult,extra:Partial<Pick<DiagnosticEvent,'snapshotHash'|'persistedHash'|'workspaceId'|'writerId'|'operationId'|'writeSource'|'directoryName'|'baseHash'|'diskHashAfterWrite'|'diskHashOnSwitch'|'revision'|'baseRevision'|'fileSize'|'fileLastModified'|'readbackSource'|'targetFile'|'pendingFile'|'commitPhase'|'rollbackDetected'|'conflictCode'|'errorCode'|'expectedRevision'|'actualRevision'|'serviceWorkspace'|'servicePid'|'requestSource'|'conflictStage'>>={}){
  const id=extra.workspaceId??await workspaceId(workspace);const unlock=await acquireWorkspaceLock(`diagnostics:${id}`);
  try {
    const dir=await workspace.getDirectoryHandle(DIAGNOSTIC_DIR,{create:true});const runs=await dir.getDirectoryHandle('runs',{create:true});const run=await runs.getDirectoryHandle(diagnosticRunId,{create:true});
    const file=await run.getFileHandle('events.jsonl',{create:true});let previous='';try{previous=await (await file.getFile()).text()}catch{}
    const row:DiagnosticEvent={schemaVersion:1,runId:diagnosticRunId,sequence:++diagnosticSequence,timestamp:now(),event,phase,projectId:project.projectId,editVersion,rectangleCount:project.rectangles.length,result,...extra,workspaceId:id,writerId:extra.writerId??writerId};
    await putFile(run,'events.jsonl',previous+JSON.stringify(row)+'\n');
    return diagnosticRunId;
  } finally {await unlock()}
}
export async function makeThumbnail(blob:Blob):Promise<Blob>{const bitmap=await createImageBitmap(blob);const side=192;const canvas=document.createElement('canvas');canvas.width=side;canvas.height=side;const c=canvas.getContext('2d')!;c.fillStyle='#fff';c.fillRect(0,0,side,side);const scale=Math.min(side/bitmap.width,side/bitmap.height);c.drawImage(bitmap,(side-bitmap.width*scale)/2,(side-bitmap.height*scale)/2,bitmap.width*scale,bitmap.height*scale);bitmap.close();return new Promise((resolve,reject)=>canvas.toBlob(v=>v?resolve(v):reject(Error('缩略图生成失败')),'image/png'))}
export async function imageDimensions(blob:Blob){const bitmap=await createImageBitmap(blob);const d={width:bitmap.width,height:bitmap.height};bitmap.close();if(d.width<1||d.height<1||d.width>20000||d.height>20000)throw Error('图片尺寸无效或过大');return d}
export function checkImage(file:File){if(!['image/png','image/jpeg','image/webp'].includes(file.type))throw Error('仅支持 PNG、JPEG、WebP 图片');if(file.size>100*1024*1024)throw Error('图片超过 100 MB')}
export async function newProjectFromImage(file:File):Promise<ProjectView>{checkImage(file);const bytes=await file.arrayBuffer();const blob=new Blob([bytes],{type:file.type});const dims=await imageDimensions(blob);const created=now(),id=uid();const ext=file.type==='image/jpeg'?'jpg':file.type.split('/')[1];const title=file.name.replace(/\.[^.]+$/,'').trim().slice(0,200)||'未命名项目';const project:Project={schemaVersion:1,projectId:id,title,createdAt:created,updatedAt:created,lastOpenedAt:created,sourceImage:{path:`assets/original.${ext}`,mimeType:file.type,...dims},canvas:{width:dims.width,height:dims.height,background:'#ffffff'},rectangles:[]};const thumb=await makeThumbnail(blob);return{project,imageBlob:blob,imageUrl:URL.createObjectURL(blob),thumbnailUrl:URL.createObjectURL(thumb)}}
export async function saveProject(workspace:WorkspaceHandle,view:ProjectView,writeImage=true,operationId=uid()):Promise<Project>{
  const p=structuredClone(validateProject(view.project));let phase:PersistencePhase='idle';phase=transitionPersistence(phase,'locking');let lock:(()=>Promise<void>)|undefined;
  try {
    lock=await acquireWorkspaceLock(`workspace:${await workspaceId(workspace)}`);phase=transitionPersistence(phase,'locating');
    const nativeCurrent=await nativeReadProjectRecord(p.projectId).catch(error=>{if(error.code==='PROJECT_NOT_FOUND')return null;throw error});
    let target=nativeCurrent?.project.directoryName??dirBase(p);
    let dir:FileSystemDirectoryHandle;
    if(nativeCurrent){dir=await workspace.getDirectoryHandle(target)}else{
      let suffix=1;
      while(true){
        try{await workspace.getDirectoryHandle(target);target=`${dirBase(p)}_${suffix++}`}
        catch(error){if((error as DOMException).name!=='NotFoundError')throw error;dir=await workspace.getDirectoryHandle(target,{create:true});break}
      }
    }
    const expectedRevision=p.revision??0;
    if(writeImage){phase=transitionPersistence(phase,'writing_assets');const assets=await dir.getDirectoryHandle('assets',{create:true});await putFile(assets,p.sourceImage.path.split('/')[1],view.imageBlob);await putFile(assets,'thumbnail.png',await makeThumbnail(view.imageBlob))}
    phase=transitionPersistence(phase,'native_committing');
    // revision is the base version. Only the service increments it on commit.
    p.directoryName=target;p.revision=expectedRevision;p.lastWriterId=writerId;
    const nativeSaved=await nativeSaveProject(target,p,operationId);
    phase=transitionPersistence(phase,'verifying_final');
    const saved=validateProject(nativeSaved.project);
    if(saved.projectId!==p.projectId||saved.revision!==expectedRevision+1||await projectHash(saved)!==await projectHash(p))throw Error('本地服务保存校验失败');
    phase=transitionPersistence(phase,'committed');return saved;
  }catch(error){if(phase!=='failed'&&phase!=='committed')phase=transitionPersistence(phase,'failed');throw error}
  finally{if(lock)await lock()}
}
export async function saveDraft(workspace:WorkspaceHandle,view:ProjectView,writeImage=true){const p=validateProject(view.project);const dir=await workspace.getDirectoryHandle(DRAFT_DIR,{create:true});const assets=await dir.getDirectoryHandle('assets',{create:true});if(writeImage){await putFile(assets,p.sourceImage.path.split('/')[1],view.imageBlob);const thumb=await makeThumbnail(view.imageBlob);await putFile(assets,'thumbnail.png',thumb)}await putFile(dir,'project.json',JSON.stringify(p,null,2)+'\n')}
export async function readDraft(workspace:WorkspaceHandle):Promise<ProjectView|null>{try{const dir=await workspace.getDirectoryHandle(DRAFT_DIR);const p=validateProject(JSON.parse(await (await dir.getFileHandle('project.json')).getFile().then(f=>f.text())));const image=await readFile(dir,p.sourceImage.path);const dimensions=await imageDimensions(image);if(dimensions.width!==p.sourceImage.width||dimensions.height!==p.sourceImage.height)throw Error('草稿图片尺寸不一致');const thumb=await readFile(dir,'assets/thumbnail.png').catch(()=>makeThumbnail(image));return{project:p,imageBlob:image,imageUrl:URL.createObjectURL(image),thumbnailUrl:URL.createObjectURL(thumb)}}catch{return null}}
export async function clearDraft(workspace:WorkspaceHandle){await workspace.removeEntry(DRAFT_DIR,{recursive:true}).catch(()=>undefined)}
async function projectDirectory(workspace:WorkspaceHandle,project:Project):Promise<FileSystemDirectoryHandle>{if(project.directoryName){try{return await workspace.getDirectoryHandle(project.directoryName)}catch{}}return (await findDir(workspace,project.projectId)).dir}
export async function readProject(workspace:WorkspaceHandle,id:string):Promise<ProjectView>{if(!/^[a-f0-9-]{36}$/i.test(id))throw Error('项目 ID 无效');const record=await nativeReadProjectRecord(id);const p=validateProject(record.project);const dir=await projectDirectory(workspace,p);const image=await readFile(dir,p.sourceImage.path);const dimensions=await imageDimensions(image);if(dimensions.width!==p.sourceImage.width||dimensions.height!==p.sourceImage.height)throw Error('图片尺寸和项目文件不一致');const thumb=await readFile(dir,'assets/thumbnail.png').catch(()=>makeThumbnail(image));return{project:p,imageBlob:image,imageUrl:URL.createObjectURL(image),thumbnailUrl:URL.createObjectURL(thumb),baseHash:await projectHash(p)}}
export async function readProjectFileMetadata(_workspace:WorkspaceHandle,id:string):Promise<{fileSize:number;fileLastModified:number}>{const record=await nativeReadProjectRecord(id);return{fileSize:record.fileSize,fileLastModified:record.fileLastModified}}
const wait=(milliseconds:number)=>new Promise<void>(resolve=>setTimeout(resolve,milliseconds));
export async function verifyProjectDurability(workspace:WorkspaceHandle,id:string,expectedRevision:number,expectedHash?:string):Promise<{view:ProjectView;hash:string;fileSize:number;fileLastModified:number}>{
  await wait(500);
  const first=await nativeReadProjectRecord(id);const firstHash=await projectHash(first.project);
  await wait(500);
  const second=await nativeReadProjectRecord(id);const secondHash=await projectHash(second.project);
  if(first.project.revision!==expectedRevision||second.project.revision!==expectedRevision||firstHash!==secondHash||(expectedHash!==undefined&&secondHash!==expectedHash))throw Error('保存未稳定落盘：本地文件服务两次读取结果不一致');
  const view=await readProject(workspace,id);return{view,hash:secondHash,fileSize:second.fileSize,fileLastModified:second.fileLastModified};
}
export async function listProjects(workspace:WorkspaceHandle):Promise<RecentProject[]>{const items:RecentProject[]=[];for(const record of await nativeListProjects()){try{const p=validateProject(record.project);const dir=await projectDirectory(workspace,p);const thumb=await readFile(dir,'assets/thumbnail.png').catch(()=>readFile(dir,p.sourceImage.path).then(makeThumbnail));items.push({projectId:p.projectId,title:p.title,createdAt:p.createdAt,lastOpenedAt:p.lastOpenedAt,thumbnailUrl:URL.createObjectURL(thumb)})}catch{}}const fallback=items.sort((a,b)=>b.createdAt.localeCompare(a.createdAt)||a.projectId.localeCompare(b.projectId));const saved=await readProjectOrder(workspace);if(!saved){await saveProjectOrder(workspace,fallback.map(item=>item.projectId));return fallback}const byId=new Map(fallback.map(item=>[item.projectId,item]));const ordered=saved.flatMap(id=>{const item=byId.get(id);if(item){byId.delete(id);return [item]}return []});const normalized=ordered.concat([...byId.values()]);if(JSON.stringify(saved)!==JSON.stringify(normalized.map(item=>item.projectId)))await saveProjectOrder(workspace,normalized.map(item=>item.projectId));return normalized}
export async function deleteProject(workspace:WorkspaceHandle,id:string){const found=await findDir(workspace,id);await workspace.removeEntry(found.name,{recursive:true})}
export async function exportZip(view:ProjectView){const p=validateProject(view.project);const zip=new JSZip();zip.file('project.json',JSON.stringify(p,null,2));zip.file(p.sourceImage.path,view.imageBlob);zip.file('assets/thumbnail.png',await makeThumbnail(view.imageBlob));return zip.generateAsync({type:'blob'})}
export async function importZip(file:File):Promise<ProjectView>{if(file.size>110*1024*1024)throw Error('ZIP 文件过大');const zip=await JSZip.loadAsync(file);const paths=Object.keys(zip.files);if(paths.some(path=>path.startsWith('/')||path.includes('..')||path.includes('\\')||path.split('/').length>3))throw Error('ZIP 包含非法路径');const entry=zip.file('project.json');if(!entry)throw Error('ZIP 缺少 project.json');const p=validateProject(JSON.parse(await entry.async('string')));const imageEntry=zip.file(p.sourceImage.path);if(!imageEntry)throw Error('ZIP 缺少原图');const bytes=await imageEntry.async('uint8array');if(bytes.byteLength>100*1024*1024)throw Error('图片过大');const copy=new Uint8Array(bytes.byteLength);copy.set(bytes);const blob=new Blob([copy.buffer],{type:p.sourceImage.mimeType});const dims=await imageDimensions(blob);if(dims.width!==p.sourceImage.width||dims.height!==p.sourceImage.height)throw Error('图片尺寸不匹配');const thumb=await makeThumbnail(blob);p.projectId=uid();p.directoryName=undefined;p.revision=0;p.lastWriterId=undefined;p.lastOpenedAt=now();p.updatedAt=now();return{project:p,imageBlob:blob,imageUrl:URL.createObjectURL(blob),thumbnailUrl:URL.createObjectURL(thumb)}}
export function downloadBlob(blob:Blob,name:string){const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),30000)}

