import { useEffect, useMemo, useState, type ReactNode } from 'react'

const API = import.meta.env.VITE_API_BASE_URL || window.location.origin
function apiFetch(input:RequestInfo|URL,init:RequestInit={}) {
  const headers=new Headers(init.headers)
  const apiKey=sessionStorage.getItem('usim-api-key')||''
  if(apiKey)headers.set('X-Simulator-Api-Key',apiKey)
  return fetch(input,{...init,headers}).then(response=>{
    if(response.status===401)window.dispatchEvent(new Event('simulator-auth-required'))
    return response
  })
}
type Flow = 'sync' | 'async'
type Format = 'json' | 'xml'
type CaptureSource = 'json' | 'xml' | 'header' | 'query' | 'path'
type ValueSource = 'fixed' | 'capture' | 'random_int' | 'random_choice' | 'random_string'
type Capture = { name: string; source: CaptureSource; path: string }
type OutputField = {
  name: string; path: string; source: ValueSource; capture: string; value: string; valueType: 'string' | 'number' | 'boolean' | 'null'
  minimum: number; maximum: number; choices: string; length: number; alphabet: string
}
type ReplyForm = { status: number; format: Format; body: string; sampleBody: string; fields: OutputField[] }
type ScenarioForm = {
  key: string; name: string; method: string; pathTemplate: string; flow: Flow; requestFormat: Format; response: ReplyForm; ack: ReplyForm; callback: ReplyForm
  callbackUrl: string; delayMs: number; maxAttempts: number; retryDelayMs: number; correlationCapture: string; namespaces: string; requestSample: string; captures: Capture[]
}
type SampleField = { path: string; label: string; value: any; valueType: OutputField['valueType']; childIndexes?: number[] }
type ParsedSample = { fields: SampleField[]; namespaces: Record<string,string>; error: string }
type XmlTreeNode = { name: string; path: string; value: string; children: XmlTreeNode[]; occurrence?: number; occurrenceCount?: number }
type JsonTreeNode = { name: string; path: string; value: string; valueType: OutputField['valueType']; children: JsonTreeNode[] }
type ScenarioSummary = { key: string; name: string; version: number; flow: Flow }
type CallbackJob = { id: number; scenario_key: string; scenario_version: number; correlation_id: string; state: string; attempts: number; due_at: string; last_error?: string }
type SimulatorMetrics = {
  sampled_at: string
  throughput: { window_seconds: number; requests: number; successful: number; failed: number; request_tps: number; successful_tps: number; total_requests: number; total_successful: number; total_failed: number }
  resources: { api: { cpu_percent: number; memory_bytes: number; memory_mb: number }; callback_worker: null | { cpu_percent: number; memory_bytes: number; memory_mb: number; updated_at: string } }
  callback_worker: { enabled: boolean }
  callback_jobs: { pending: number; retry_wait: number; in_progress: number; succeeded: number; failed: number; queue_depth: number; failure_reasons: {reason:string;count:number}[] }
}

const blankOutput = (): OutputField => ({name:'',path:'',source:'fixed',capture:'',value:'',valueType:'string',minimum:0,maximum:100,choices:'SUCCESS,DECLINED',length:12,alphabet:'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'})
const blankReply = (format: Format = 'json'): ReplyForm => ({status:200,format,body:format==='xml'?'<?xml version="1.0"?><Response></Response>':'',sampleBody:'',fields:[]})
const newForm = (): ScenarioForm => ({key:'new-scenario',name:'New scenario',method:'POST',pathTemplate:'',flow:'sync',requestFormat:'json',response:blankReply(),ack:{...blankReply(),status:202},callback:blankReply(),callbackUrl:'http://host.docker.internal:9000/callback',delayMs:1000,maxAttempts:1,retryDelayMs:1000,correlationCapture:'',namespaces:'{}',requestSample:'',captures:[]})

function pathParameterNames(template:string):string[] {
  return Array.from(template.matchAll(/\{([A-Za-z_][A-Za-z0-9_]*)\}/g),match=>match[1])
}

function extractBody(sample: string): string {
  const crlf = sample.indexOf('\r\n\r\n')
  if (crlf >= 0) return sample.slice(crlf + 4).trim()
  const lf = sample.indexOf('\n\n')
  return lf >= 0 && /^(POST|PUT|PATCH|GET|HTTP\/|Content-Type:)/i.test(sample.trim()) ? sample.slice(lf + 2).trim() : sample.trim()
}

function safeToken(path: string): string {
  const token = path.replace(/^\$\.?/, '').replace(/[^a-zA-Z0-9_]+/g, '_').replace(/^_+|_+$/g, '') || 'value'
  return /^\d/.test(token) ? `field_${token}` : token
}

function parseSample(format: Format, source: string): ParsedSample {
  const fields: SampleField[] = []
  const namespaces: Record<string,string> = {}
  const body = extractBody(source)
  if (!body) return {fields,namespaces,error:''}
  try {
    if (format === 'json') {
      const root = JSON.parse(body)
      const walk = (value: any, path: string) => {
        if (Array.isArray(value)) value.forEach((child,index)=>walk(child,path?`${path}.${index}`:String(index)))
        else if (value && typeof value === 'object') Object.entries(value).forEach(([key,child])=>walk(child,path?`${path}.${key}`:key))
        else fields.push({path:path||'$',label:path||'$',value,valueType:value===null?'null':typeof value==='number'?'number':typeof value==='boolean'?'boolean':'string'})
      }
      walk(root,'')
      return {fields,namespaces,error:''}
    }

    const document = new DOMParser().parseFromString(body,'application/xml')
    if (document.getElementsByTagName('parsererror').length) throw new Error('XML could not be parsed. Check that the pasted body is well formed.')
    const root = document.documentElement
    if (!root) throw new Error('No XML root element was found.')
    const prefixes = new Map<string,string>()
    let nextPrefix = 1
    const prefixFor = (element: Element) => {
      const uri = element.namespaceURI
      if (!uri) return element.localName || element.tagName
      let prefix = prefixes.get(uri)
      if (!prefix) {
        prefix = element.prefix || `ns${nextPrefix++}`
        while (namespaces[prefix] && namespaces[prefix] !== uri) prefix = `ns${nextPrefix++}`
        prefixes.set(uri,prefix); namespaces[prefix]=uri
      }
      return `${prefix}:${element.localName || element.tagName}`
    }
    const walkXml = (element: Element, path: string, indexes: number[]) => {
      if (element.children.length === 0) {
        const value = element.textContent || ''
        if (value.trim()) fields.push({path:path||'.',label:path||'.',value,valueType:'string',childIndexes:indexes})
        return
      }
      const children=Array.from(element.children)
      const keys=children.map(child=>`${child.namespaceURI||''}|${child.localName||child.tagName}`)
      const totals=new Map<string,number>()
      keys.forEach(key=>totals.set(key,(totals.get(key)||0)+1))
      const seen=new Map<string,number>()
      children.forEach((child,index)=>{
        const key=keys[index]
        const name=prefixFor(child)
        const occurrence=(seen.get(key)||0)+1
        seen.set(key,occurrence)
        const segment=(totals.get(key)||0)>1?`${name}[${occurrence}]`:name
        walkXml(child,path?`${path}/${segment}`:segment,[...indexes,index])
      })
    }
    walkXml(root,'.',[])
    return {fields,namespaces,error:''}
  } catch (error) {
    return {fields:[],namespaces,error:error instanceof Error?error.message:'Sample could not be parsed.'}
  }
}

function jsonTreeFromSample(source:string):{root:JsonTreeNode|null;error:string} {
  const body=extractBody(source)
  if(!body)return {root:null,error:''}
  try {
    const value=JSON.parse(body)
    const build=(item:any,name:string,path:string):JsonTreeNode=>{
      const parentPath=path==='$'?'':path
      if(Array.isArray(item))return {name,path,value:`${item.length} ${item.length===1?'item':'items'}`,valueType:'null',children:item.map((child,index)=>build(child,`[${index}]`,parentPath?`${parentPath}.${index}`:String(index)))}
      if(item&&typeof item==='object')return {name,path,value:`${Object.keys(item).length} ${Object.keys(item).length===1?'field':'fields'}`,valueType:'null',children:Object.entries(item).map(([key,child])=>build(child,key,parentPath?`${parentPath}.${key}`:key))}
      return {name,path,value:item===null?'null':String(item),valueType:item===null?'null':typeof item==='number'?'number':typeof item==='boolean'?'boolean':'string',children:[]}
    }
    return {root:build(value,Array.isArray(value)?'Array':'Object','$'),error:''}
  } catch(error) { return {root:null,error:error instanceof Error?error.message:'JSON could not be parsed.'} }
}

function displayJsonPath(path:string):string {
  if(!path||path==='$')return '$'
  return `$.${path.split('.').map((part,index)=>/^\\d+$/.test(part)?`[${part}]${index<path.split('.').length-1?'.':''}`:part).join('')}`
}

function JsonTreeBrowser({source,selectedPath,onSelect,ariaLabel}:{source:string;selectedPath:string;onSelect:(node:JsonTreeNode)=>void;ariaLabel:string}) {
  const [search,setSearch]=useState('')
  const [expanded,setExpanded]=useState<Record<string,boolean>>({'$':true})
  const parsed=useMemo(()=>jsonTreeFromSample(source),[source])
  const query=search.trim().toLowerCase()
  const matches=(node:JsonTreeNode):boolean=>!query||`${node.name} ${displayJsonPath(node.path)} ${node.value}`.toLowerCase().includes(query)||node.children.some(matches)
  const render=(node:JsonTreeNode,depth=0):ReactNode=>{
    if(!matches(node))return null
    if(node.children.length) {
      const isExpanded=query?true:Boolean(expanded[node.path])
      return <div className="xml-tree-branch" key={node.path}>
        <button type="button" className="json-tree-branch-toggle" aria-expanded={isExpanded} onClick={()=>setExpanded(current=>({...current,[node.path]:!isExpanded}))}><span>{isExpanded?'▾':'▸'}</span><strong>{node.name}</strong><small>{node.value}</small></button>
        {isExpanded&&<div className="xml-tree-children">{node.children.map(child=>render(child,depth+1))}</div>}
      </div>
    }
    return <button type="button" key={node.path} className={`xml-tree-leaf ${selectedPath===node.path?'selected':''}`} onClick={()=>onSelect(node)}>
      <span className="xml-tree-leaf-name">{node.name}</span><code title={node.value}>{node.value||'(empty)'} · {node.valueType}</code>{selectedPath===node.path&&<span className="xml-tree-mapped">Selected</span>}
    </button>
  }
  return <div className="response-tree-panel json-field-browser">
    <div className="response-tree-tools"><input aria-label={`Search ${ariaLabel}`} placeholder="Search JSON fields by name, path, or value" value={search} onChange={event=>setSearch(event.target.value)}/><span>{parsed.root?'Expand objects/arrays, then select a value':parsed.error||'Paste a valid JSON sample first'}</span></div>
    {parsed.root&&<div className="response-tree-scroll" role="tree" aria-label={ariaLabel}>{render(parsed.root)}</div>}
    {parsed.error&&<p className="json-tree-error">{parsed.error}</p>}
  </div>
}

function outputFieldsFromSample(format: Format, sample: string): OutputField[] {
  const parsed=parseSample(format,sample)
  if (parsed.error) throw new Error(parsed.error)
  const used=new Set<string>()
  return parsed.fields.map(field=>{
    let name=safeToken(field.path)
    let suffix=2
    while(used.has(name)){name=`${safeToken(field.path)}_${suffix++}`}
    used.add(name)
    return {...blankOutput(),name,path:field.path,source:'fixed' as ValueSource,value:field.value==null?'':String(field.value),valueType:field.valueType}
  })
}

function bodyFields(value: any, prefix = '', result: Map<string,string> = new Map()): Map<string,string> {
  if (typeof value === 'string') {
    const exact = value.match(/^\{\{([^{}]+)\}\}$/)
    if (exact) result.set(exact[1], prefix)
  } else if (Array.isArray(value)) value.forEach((item, i) => bodyFields(item, prefix ? `${prefix}.${i}` : String(i), result))
  else if (value && typeof value === 'object') Object.entries(value).forEach(([key, child]) => bodyFields(child, prefix ? `${prefix}.${key}` : key, result))
  return result
}

function replyFromDefinition(reply: any, format: Format): ReplyForm {
  const body = reply?.body ?? (format === 'xml' ? '<?xml version="1.0"?><Response></Response>' : {})
  const paths = bodyFields(body)
  const xmlPaths = new Map<string,string>()
  if(format==='xml'&&typeof body==='string'){
    const parsed=parseSample('xml',body)
    for(const field of parsed.fields){const token=String(field.value).match(/^\{\{([^{}]+)\}\}$/);if(token)xmlPaths.set(token[1],field.path)}
  }
  const fields: OutputField[] = Object.entries(reply?.fields || {}).map(([name, rule]: [string, any]) => ({
    ...blankOutput(), name, path: paths.get(name) || xmlPaths.get(name) || name, source: rule.source || 'fixed', capture: rule.capture || '',
    value: rule.value == null ? '' : String(rule.value), valueType: rule.value === null ? 'null' : typeof rule.value === 'number' ? 'number' : typeof rule.value === 'boolean' ? 'boolean' : 'string',
    minimum: rule.minimum ?? 0, maximum: rule.maximum ?? 100, choices: (rule.choices || []).join(','), length: rule.length ?? 12, alphabet: rule.alphabet || blankOutput().alphabet,
  }))
  if (format === 'json') {
    const flatten = (value: any, prefix = '') => {
      if (value && typeof value === 'object' && !Array.isArray(value)) {
        Object.entries(value).forEach(([key, child]) => flatten(child, prefix ? `${prefix}.${key}` : key))
      } else if (typeof value !== 'string' || !/^\{\{([^{}]+)\}\}$/.test(value)) {
        if (prefix && !fields.some(field => field.path === prefix)) {
          const fixed = blankOutput(); fixed.name = `fixed_${fields.length + 1}`; fixed.path = prefix; fixed.source = 'fixed'; fixed.value = String(value ?? '');
          fixed.valueType = value === null ? 'null' : typeof value === 'number' ? 'number' : typeof value === 'boolean' ? 'boolean' : 'string'; fields.push(fixed)
        }
      }
    }
    flatten(body)
  }
  const sampleBody=reply?.sample_body ?? (format==='json'?JSON.stringify(body,null,2):'')
  return {status:reply?.status_code ?? 200,format,body:format==='xml'?(typeof body==='string'?body:''): '',sampleBody,fields}
}

function formFromDefinition(definition: any): ScenarioForm {
  const requestFormat: Format = definition.request_format || 'json'
  const responseFormat: Format = definition.response_format || 'json'
  const ackFormat: Format = definition.ack_format || 'json'
  const callbackFormat: Format = definition.callback_format || 'json'
  const asyncConfig = definition.async_config || {}
  return {
    key:definition.key || '', name:definition.name || '', method:definition.method || 'POST', pathTemplate:definition.path_template || '', flow:definition.flow || 'sync', requestFormat,
    response:replyFromDefinition(definition.response,responseFormat), ack:replyFromDefinition(definition.ack,ackFormat), callback:replyFromDefinition(definition.callback,callbackFormat),
    callbackUrl:asyncConfig.callback_url || '', delayMs:asyncConfig.delay_ms ?? 1000, maxAttempts:asyncConfig.max_attempts ?? 1,
    retryDelayMs:asyncConfig.retry_delay_ms ?? 1000, correlationCapture:asyncConfig.correlation_capture || '',
    namespaces:JSON.stringify(definition.namespaces || {},null,2),requestSample:definition.request_sample || '',
    captures:(definition.captures || []).map((capture: any) => ({name:capture.name,source:capture.source,path:capture.path})),
  } as ScenarioForm
}

function setJsonPath(rootValue:any,path:string,value:any):any {
  const normalized=path.replace(/^\$\.?/,'')
  if(!normalized)return value
  const parts=normalized.split('.').filter(Boolean)
  let root=rootValue&&typeof rootValue==='object'?rootValue:{}
  let cursor:any=root
  parts.forEach((part,index)=>{
    const key=Array.isArray(cursor)?Number(part):part
    if(index===parts.length-1)cursor[key]=value
    else {
      const nextIsIndex=/^\d+$/.test(parts[index+1])
      if(cursor[key]===undefined||cursor[key]===null||typeof cursor[key]!=='object')cursor[key]=nextIsIndex?[]:{}
      cursor=cursor[key]
    }
  })
  return root
}

function xmlTemplateFromFields(reply:ReplyForm):string {
  const sample=extractBody(reply.sampleBody)
  if(!sample)return reply.body
  try {
    const document=new DOMParser().parseFromString(sample,'application/xml')
    if(document.getElementsByTagName('parsererror').length)return reply.body||sample
    const parsed=parseSample('xml',sample)
    const root=document.documentElement
    for(const field of reply.fields){
      const match=parsed.fields.find(item=>item.path===field.path)
      if(!match?.childIndexes||!field.name.trim())continue
      let element:Element=root
      for(const childIndex of match.childIndexes)element=element.children[childIndex]
      element.textContent=`{{${field.name}}}`
    }
    const declaration=sample.match(/^\s*(<\?xml[^?]*\?>)/i)?.[1]||''
    return `${declaration}${new XMLSerializer().serializeToString(root)}`
  } catch { return reply.body||sample }
}

function prettyPrintXml(source:string):string {
  const xml=source.trim()
  if(!xml)return ''
  try {
    const document=new DOMParser().parseFromString(xml,'application/xml')
    if(document.getElementsByTagName('parsererror').length)return source
    const serializer=new XMLSerializer()
    const declaration=xml.match(/^\s*(<\?xml[^?]*\?>)/i)?.[1]
    const render=(element:Element,depth:number):string=>{
      const pad='  '.repeat(depth)
      const opening=serializer.serializeToString(element.cloneNode(false))
      const children=Array.from(element.childNodes)
      const hasElements=children.some(child=>child.nodeType===Node.ELEMENT_NODE)
      const hasText=children.some(child=>(child.nodeType===Node.TEXT_NODE||child.nodeType===Node.CDATA_SECTION_NODE)&&(child.textContent||'').trim())
      if(!hasElements||hasText)return `${pad}${serializer.serializeToString(element)}`
      const start=opening.replace(/\s*\/>$/,'>')
      const end=`</${element.tagName}>`
      const nested=children.filter(child=>child.nodeType!==Node.TEXT_NODE||Boolean((child.textContent||'').trim())).map(child=>child.nodeType===Node.ELEMENT_NODE?render(child as Element,depth+1):`${'  '.repeat(depth+1)}${serializer.serializeToString(child)}`)
      return `${pad}${start}\n${nested.join('\n')}\n${pad}${end}`
    }
    return [declaration,render(document.documentElement,0)].filter(Boolean).join('\n')
  } catch { return source }
}

function xmlTreeFromSample(source:string):{root:XmlTreeNode|null;error:string} {
  const body=extractBody(source)
  if(!body)return {root:null,error:''}
  try {
    const document=new DOMParser().parseFromString(body,'application/xml')
    if(document.getElementsByTagName('parsererror').length)return {root:null,error:'XML could not be parsed.'}
    const namespaces:Record<string,string>={}
    const prefixes=new Map<string,string>()
    let nextPrefix=1
    const nameOf=(element:Element)=>{
      if(!element.namespaceURI)return element.localName||element.tagName
      let prefix=prefixes.get(element.namespaceURI)
      if(!prefix){prefix=element.prefix||`ns${nextPrefix++}`;while(namespaces[prefix]&&namespaces[prefix]!==element.namespaceURI)prefix=`ns${nextPrefix++}`;prefixes.set(element.namespaceURI,prefix);namespaces[prefix]=element.namespaceURI}
      return `${prefix}:${element.localName||element.tagName}`
    }
    const build=(element:Element,path:string):XmlTreeNode=>{
      const children=Array.from(element.children)
      const keys=children.map(child=>`${child.namespaceURI||''}|${child.localName||child.tagName}`)
      const totals=new Map<string,number>()
      keys.forEach(key=>totals.set(key,(totals.get(key)||0)+1))
      const seen=new Map<string,number>()
      const nodes=children.map((child,index)=>{
        const key=keys[index]
        const name=nameOf(child)
        const occurrence=(seen.get(key)||0)+1
        seen.set(key,occurrence)
        const repeated=(totals.get(key)||0)>1
        const segment=repeated?`${name}[${occurrence}]`:name
        const childPath=path==='.'?`./${segment}`:`${path}/${segment}`
        const node=build(child,childPath)
        if(repeated){node.occurrence=occurrence;node.occurrenceCount=totals.get(key)}
        return node
      })
      return {name:nameOf(element),path,value:children.length?'':(element.textContent||'').trim(),children:nodes}
    }
    return {root:build(document.documentElement,'.'),error:''}
  } catch { return {root:null,error:'Could not build the XML response tree.'} }
}

function bodyFromFields(reply: ReplyForm): any {
  if (reply.format === 'xml') return xmlTemplateFromFields(reply)
  let root:any={}
  try { root=reply.sampleBody.trim()?JSON.parse(extractBody(reply.sampleBody)):{}} catch { root={} }
  for (const field of reply.fields) {
    if (!field.name.trim() || !field.path.trim()) continue
    root=setJsonPath(root,field.path,`{{${field.name}}}`)
  }
  return root
}

function ResponseStructure({reply,captures,onChange}:{reply:ReplyForm;captures:Capture[];onChange:(value:ReplyForm)=>void}) {
  const [search,setSearch]=useState('')
  const [selectedPath,setSelectedPath]=useState('')
  const [expandAll,setExpandAll]=useState(false)
  const parsed=useMemo(()=>xmlTreeFromSample(reply.sampleBody),[reply.sampleBody])
  const query=search.trim().toLowerCase()
  const matches=(node:XmlTreeNode):boolean=>!query||`${node.name} ${node.path} ${node.value}`.toLowerCase().includes(query)||node.children.some(matches)
  const selected=findXmlTreeNode(parsed.root,selectedPath)
  const selectedField=reply.fields.find(field=>field.path===selectedPath)
  const changeCapture=(capture:string)=>{
    if(!selectedField)return
    onChange({...reply,fields:reply.fields.map(field=>field.path===selectedPath?{...field,source:capture?'capture':'fixed',capture}:field)})
  }
  const renderNode=(node:XmlTreeNode,depth=0):ReactNode=>{
    if(!matches(node))return null
    if(node.children.length){
      return <details className="xml-tree-branch" key={node.path} open={expandAll||Boolean(query)||depth<1}>
        <summary><span className="xml-tree-name">{node.name}</span><small>{node.children.length} {node.children.length===1?'element':'elements'}</small></summary>
        <div className="xml-tree-children">{node.children.map(child=>renderNode(child,depth+1))}</div>
      </details>
    }
    const canMap=Boolean(selectedField)
    return <button type="button" key={node.path} className={`xml-tree-leaf ${selectedPath===node.path?'selected':''}`} onClick={()=>setSelectedPath(node.path)}>
      <span className="xml-tree-leaf-name">{node.name}{node.occurrence?` · ${node.occurrence}/${node.occurrenceCount}`:''}</span>
      <code>{node.value||'(empty)'}</code>
      {canMap&&selectedPath===node.path&&<span className="xml-tree-mapped">Selected</span>}
    </button>
  }
  if(reply.format!=='xml')return null
  return <section className="response-tree-panel">
    <div className="response-tree-heading"><div><h4>Response structure and mapping</h4><p>Find an XML element, select it, then map it to a captured request value. Repeated siblings show their position.</p></div><button type="button" className="secondary-button" onClick={()=>setExpandAll(value=>!value)}>{expandAll?'Collapse all':'Expand all'}</button></div>
    <div className="response-tree-tools"><input aria-label="Search response elements" placeholder="Search by element, path, or sample value" value={search} onChange={event=>setSearch(event.target.value)}/><span>{parsed.root?`${reply.fields.length} mapped fields configured`:parsed.error||'Paste a valid XML response sample to browse fields'}</span></div>
    {parsed.root&&<div className="response-tree-scroll" role="tree" aria-label="XML response fields">{renderNode(parsed.root)}</div>}
    {selected&&<div className="selected-response-field"><div><strong>{selected.name}{selected.occurrence?` · repeated item ${selected.occurrence} of ${selected.occurrenceCount}`:''}</strong><code>{selected.path}</code></div>
      {selectedField?<label>Use request capture<select value={selectedField.source==='capture'?selectedField.capture:''} onChange={event=>changeCapture(event.target.value)}><option value="">Keep configured response value</option>{captures.map(capture=><option key={capture.name} value={capture.name}>{capture.name}</option>)}</select></label>:<p>Generate response fields from the sample first, then this element can be mapped.</p>}
    </div>}
  </section>
}

function findXmlTreeNode(root:XmlTreeNode|null,path:string):XmlTreeNode|null {
  if(!root||!path)return null
  if(root.path===path)return root
  for(const child of root.children){const found=findXmlTreeNode(child,path);if(found)return found}
  return null
}

function JsonResponseStructure({reply,captures,onChange}:{reply:ReplyForm;captures:Capture[];onChange:(value:ReplyForm)=>void}) {
  const [selectedPath,setSelectedPath]=useState('')
  const parsed=useMemo(()=>jsonTreeFromSample(reply.sampleBody),[reply.sampleBody])
  const selected=findJsonTreeNode(parsed.root,selectedPath)
  const selectedField=reply.fields.find(field=>field.path===selectedPath)
  const addMapping=()=>{
    if(!selected||selectedField)return
    const name=safeToken(selected.path)
    const uniqueName=reply.fields.some(field=>field.name===name)?`${name}_${reply.fields.length+1}`:name
    onChange({...reply,fields:[...reply.fields,{...blankOutput(),name:uniqueName,path:selected.path,value:selected.value,valueType:selected.valueType}]})
  }
  const changeCapture=(capture:string)=>{
    if(!selectedField)return
    onChange({...reply,fields:reply.fields.map(field=>field.path===selectedPath?{...field,source:capture?'capture':'fixed',capture}:field)})
  }
  if(reply.format!=='json')return null
  return <section className="response-tree-panel">
    <div className="response-tree-heading"><div><h4>JSON structure and mapping</h4><p>Browse large JSON samples, select an individual response value, and map it to a request capture.</p></div></div>
    <JsonTreeBrowser source={reply.sampleBody} selectedPath={selectedPath} onSelect={node=>setSelectedPath(node.path)} ariaLabel="JSON response fields"/>
    {selected&&<div className="selected-response-field"><div><strong>{selected.name}</strong><code>{displayJsonPath(selected.path)} · {selected.valueType}</code></div>
      {selectedField?<label>Use request capture<select value={selectedField.source==='capture'?selectedField.capture:''} onChange={event=>changeCapture(event.target.value)}><option value="">Keep configured response value</option>{captures.map(capture=><option key={capture.name} value={capture.name}>{capture.name}</option>)}</select></label>:<button type="button" className="secondary-button" onClick={addMapping}>Add mapping for this field</button>}
    </div>}
  </section>
}

function findJsonTreeNode(root:JsonTreeNode|null,path:string):JsonTreeNode|null {
  if(!root||!path)return null
  if(root.path===path)return root
  for(const child of root.children){const found=findJsonTreeNode(child,path);if(found)return found}
  return null
}

function JsonCapturePicker({source,path,onSelect}:{source:string;path:string;onSelect:(path:string)=>void}) {
  const selected=useMemo(()=>jsonTreeFromSample(source),[source])
  const field=selected.root?findJsonTreeNode(selected.root,path):null
  return <details className="json-capture-picker">
    <summary>{field?`Selected: ${displayJsonPath(field.path)}`:'Browse JSON fields'}</summary>
    <JsonTreeBrowser source={source} selectedPath={path} onSelect={node=>onSelect(node.path)} ariaLabel="JSON request fields"/>
  </details>
}

function valueRule(field: OutputField): any {
  if (field.source === 'fixed') {
    let value: any = field.value
    if (field.valueType === 'number') value = Number(field.value)
    if (field.valueType === 'boolean') value = field.value.toLowerCase() === 'true'
    if (field.valueType === 'null') value = null
    return {source:'fixed',value}
  }
  if (field.source === 'capture') return {source:'capture',capture:field.capture}
  if (field.source === 'random_int') return {source:'random_int',minimum:Number(field.minimum),maximum:Number(field.maximum)}
  if (field.source === 'random_choice') return {source:'random_choice',choices:field.choices.split(',').map(value=>value.trim()).filter(Boolean)}
  return {source:'random_string',length:Number(field.length),alphabet:field.alphabet}
}

function replyDefinition(reply: ReplyForm) {
  const mediaType = reply.format === 'xml' ? 'text/xml; charset=utf-8' : 'application/json'
  return {status_code:Number(reply.status),content_type:mediaType,headers:{},body:bodyFromFields(reply),sample_body:reply.sampleBody||null,fields:Object.fromEntries(reply.fields.filter(field=>field.name.trim()).map(field=>[field.name,valueRule(field)]))}
}

function definitionFromForm(form: ScenarioForm) {
  let namespaces: Record<string,string> = {}
  try { namespaces = JSON.parse(form.namespaces || '{}') } catch { throw new Error('Namespaces must be valid JSON, for example {"soapenv":"http://schemas.xmlsoap.org/soap/envelope/"}.') }
  const definition: any = {
    key:form.key.trim(),name:form.name.trim(),flow:form.flow,request_format:form.requestFormat,method:form.method,path_template:form.pathTemplate.trim(),
    captures:form.captures.filter(capture=>capture.name.trim() && capture.path.trim()),request_sample:form.requestSample||null,namespaces,
    response_format:form.response.format,response:replyDefinition(form.response),ack_format:form.ack.format,ack:replyDefinition(form.ack),
    callback_format:form.callback.format,callback:replyDefinition(form.callback),
  }
  if (form.flow === 'async') definition.async_config = {callback_url:form.callbackUrl,delay_ms:Number(form.delayMs),max_attempts:Number(form.maxAttempts),retry_delay_ms:Number(form.retryDelayMs),correlation_capture:form.correlationCapture}
  return definition
}

function OutputFields({reply,captures,onChange}:{reply:ReplyForm;captures:Capture[];onChange:(value:ReplyForm)=>void}) {
  const [advancedOpen,setAdvancedOpen]=useState(reply.format==='json')
  useEffect(()=>setAdvancedOpen(reply.format==='json'),[reply.format])
  const change = (index:number,patch:Partial<OutputField>) => onChange({...reply,fields:reply.fields.map((field,i)=>i===index?{...field,...patch}:field)})
  return <div className="output-section">
    <div className="table-heading"><div><h4>Response values</h4><p>Map values into the payload. Use the field name as a token in XML, or as a JSON value path.</p></div><button type="button" onClick={()=>onChange({...reply,fields:[...reply.fields,blankOutput()]})}>+ Add value</button></div>
    {reply.fields.length===0 && <div className="empty-hint">No dynamic values. Add a value to copy request data or generate a fixed/random response value.</div>}
    <details className="advanced-output-fields" open={advancedOpen} onToggle={event=>setAdvancedOpen(event.currentTarget.open)}>
      <summary>Advanced response value rules ({reply.fields.length})</summary>
      {reply.fields.map((field,index)=><div className="value-card" key={index}>
      <div className="value-top"><strong>Value {index+1}</strong><button type="button" className="link-button danger" onClick={()=>onChange({...reply,fields:reply.fields.filter((_,i)=>i!==index)})}>Remove</button></div>
      <div className="value-grid">
        <label>Token / field name<input value={field.name} placeholder="providerReference" onChange={e=>change(index,{name:e.target.value})}/></label>
        {reply.format==='json' && <label>JSON path<input value={field.path} placeholder="result.reference" onChange={e=>change(index,{path:e.target.value})}/></label>}
        <label>Value source<select value={field.source} onChange={e=>change(index,{source:e.target.value as ValueSource})}><option value="fixed">Fixed value</option><option value="capture">Copy from request</option><option value="random_int">Random number</option><option value="random_choice">Random choice</option><option value="random_string">Random string</option></select></label>
        {field.source==='fixed' && <><label>Value<input value={field.value} placeholder="SUCCESS" onChange={e=>change(index,{value:e.target.value})}/></label><label>Data type<select value={field.valueType} onChange={e=>change(index,{valueType:e.target.value as OutputField['valueType']})}><option value="string">Text</option><option value="number">Number</option><option value="boolean">True / false</option><option value="null">Null</option></select></label></>}
        {field.source==='capture' && <label>Request value<select value={field.capture} onChange={e=>change(index,{capture:e.target.value})}><option value="">Choose a capture</option>{captures.map(capture=><option key={capture.name} value={capture.name}>{capture.name}</option>)}</select></label>}
        {field.source==='random_int' && <><label>Minimum<input type="number" value={field.minimum} onChange={e=>change(index,{minimum:Number(e.target.value)})}/></label><label>Maximum<input type="number" value={field.maximum} onChange={e=>change(index,{maximum:Number(e.target.value)})}/></label></>}
        {field.source==='random_choice' && <label className="wide">Allowed values<input value={field.choices} placeholder="SUCCESS,DECLINED,PENDING" onChange={e=>change(index,{choices:e.target.value})}/><small>Separate choices with commas.</small></label>}
        {field.source==='random_string' && <><label>Length<input type="number" min="1" max="256" value={field.length} onChange={e=>change(index,{length:Number(e.target.value)})}/></label><label>Allowed characters<input value={field.alphabet} onChange={e=>change(index,{alphabet:e.target.value})}/></label></>}
      </div>
      </div>)}
    </details>
  </div>
}

function ReplyEditor({title,reply,captures,onChange}:{title:string;reply:ReplyForm;captures:Capture[];onChange:(value:ReplyForm)=>void}) {
  const [sampleError,setSampleError]=useState('')
  const parsedSample=useMemo(()=>parseSample(reply.format,reply.sampleBody),[reply.format,reply.sampleBody])
  const generateFields=()=>{
    try { onChange({...reply,fields:outputFieldsFromSample(reply.format,reply.sampleBody)});setSampleError('') }
    catch(error){setSampleError(error instanceof Error?error.message:'Could not parse the pasted response sample.')}
  }
  return <section className="subpanel">
    <div className="section-title"><div><h3>{title}</h3><p>Configure the HTTP/SOAP response returned for this step.</p></div></div>
    <div className="form-grid compact">
      <label>Payload format<select value={reply.format} onChange={e=>onChange({...reply,format:e.target.value as Format,body:e.target.value==='xml'?'<?xml version="1.0"?><Response></Response>':'',sampleBody:'',fields:[]})}><option value="json">JSON</option><option value="xml">XML</option></select></label>
      <label>HTTP status<input type="number" min="100" max="599" value={reply.status} onChange={e=>onChange({...reply,status:Number(e.target.value)})}/></label>
      {reply.format==='xml' && <label className="wide">XML content type<input value="text/xml; charset=utf-8" readOnly/></label>}
    </div>
    <label className="payload-label">Paste sample {reply.format.toUpperCase()} body<small>Paste a representative response. Generate fields to keep its structure and map returned values to request captures or fixed/random values.</small><textarea className={`payload-editor sample-editor ${reply.format==='xml'?'xml-editor':''}`} spellCheck={false} value={reply.sampleBody} onChange={e=>{onChange({...reply,sampleBody:e.target.value});setSampleError('')}} placeholder={reply.format==='json'?'Paste a JSON response body here':'Paste an XML / SOAP response body here'}/></label>
    <div className="sample-actions"><button type="button" onClick={generateFields} disabled={!reply.sampleBody.trim()}>{reply.format==='json'?'Generate values for all JSON fields':'Generate fields from sample'}</button><span>{parsedSample.error?`Sample needs attention: ${parsedSample.error}`:reply.format==='json'?`${parsedSample.fields.length} value fields detected · tree mapping can add only selected fields`:`${parsedSample.fields.length} fields detected`}</span>{sampleError&&<strong>{sampleError}</strong>}</div>
    <ResponseStructure reply={reply} captures={captures} onChange={onChange}/>
    <JsonResponseStructure reply={reply} captures={captures} onChange={onChange}/>
    {reply.format==='json' ? <div className="json-preview"><strong>Configured JSON response preview</strong><p>Generated from the pasted body and the response value mappings below.</p><pre>{JSON.stringify(bodyFromFields(reply),null,2)}</pre></div> : <div className="json-preview"><strong>Configured XML response preview</strong><p>Formatted for readability; the simulator response payload is unchanged.</p><pre>{prettyPrintXml(String(bodyFromFields(reply)))}</pre></div>}
    <OutputFields reply={reply} captures={captures} onChange={onChange}/>
  </section>
}

function MonitoringDashboard({metrics,error,jobs,onRefresh,onStopAndClear,onResume,actionBusy,actionMessage}:{metrics:SimulatorMetrics|null;error:string;jobs:CallbackJob[];onRefresh:()=>void;onStopAndClear:()=>void;onResume:()=>void;actionBusy:boolean;actionMessage:string}) {
  const resources=metrics?.resources
  const throughput=metrics?.throughput
  const queue=metrics?.callback_jobs
  const formatBytes=(bytes:number)=>bytes>=1024*1024?`${(bytes/(1024*1024)).toFixed(1)} MB`:`${(bytes/1024).toFixed(0)} KB`
  const cards=[
    {label:`Serving TPS · last ${throughput?.window_seconds||10}s`,value:throughput?throughput.successful_tps.toFixed(1):'—',detail:throughput?`${throughput.successful} successful of ${throughput.requests} requests in window`: 'Waiting for simulator metrics'},
    {label:'API CPU',value:resources?`${resources.api.cpu_percent.toFixed(1)}%`:'—',detail:'API process usage'},
    {label:'API memory',value:resources?`${resources.api.memory_mb.toFixed(1)} MB`:'—',detail:resources?formatBytes(resources.api.memory_bytes):'API process resident memory'},
    {label:'Callback queue',value:queue?String(queue.queue_depth):'—',detail:queue?`${queue.pending} pending · ${queue.retry_wait} retry wait`: 'Waiting for callback metrics'},
  ]
  return <section className="monitor-dashboard">
    <div className="monitor-heading"><div><div className="eyebrow">RUNTIME HEALTH</div><h2>Live monitoring</h2><p>Simulator throughput, process resources, and asynchronous callback status.</p></div><div className="monitor-actions"><span className={`live-status ${error?'offline':''}`}><i/>{error?'API unavailable':'Auto-refresh · 2 sec'}</span><button onClick={onRefresh}>Refresh now</button></div></div>
    {error&&<div className="monitor-error">{error}</div>}
    <div className="metric-grid">{cards.map(card=><article className="metric-card" key={card.label}><span>{card.label}</span><strong>{card.value}</strong><small>{card.detail}</small></article>)}</div>
    <div className="monitor-columns">
      <section className="panel monitor-panel"><div className="section-title"><div><h3>Callback worker resources</h3><p>Worker process measurements from its latest heartbeat.</p></div></div>
        <div className="callback-controls"><span className={metrics?.callback_worker.enabled===false?'worker-paused':'worker-active'}>{metrics?.callback_worker.enabled===false?'Paused':'Running'}</span>{metrics?.callback_worker.enabled===false?<button onClick={onResume} disabled={actionBusy}>{actionBusy?'Please wait…':'Resume callback worker'}</button>:<button className="clear-callback-button" onClick={onStopAndClear} disabled={actionBusy}>{actionBusy?'Please wait…':'Stop & clear all callbacks'}</button>}</div>
        {actionMessage&&<div className="callback-action-message">{actionMessage}</div>}
        {resources?.callback_worker?<div className="worker-metrics"><div><span>CPU</span><strong>{resources.callback_worker.cpu_percent.toFixed(1)}%</strong></div><div><span>Memory</span><strong>{resources.callback_worker.memory_mb.toFixed(1)} MB</strong></div><small>Updated {new Date(resources.callback_worker.updated_at).toLocaleTimeString()}</small></div>:<div className="empty-hint">Waiting for the callback worker heartbeat. Confirm the worker is running and restart it with the latest image.</div>}
        <div className="queue-grid">{[['Pending',queue?.pending],['Retry wait',queue?.retry_wait],['In progress',queue?.in_progress],['Succeeded',queue?.succeeded],['Failed',queue?.failed]].map(([label,value])=><div key={String(label)}><span>{label}</span><strong>{value??'—'}</strong></div>)}</div>
        <div className="failure-reasons"><div className="failure-reasons-heading"><strong>Failure reasons</strong><span>Top 10 · grouped by reason</span></div>
          {queue?.failure_reasons?.length?<div className="failure-reason-list">{queue.failure_reasons.map(item=><div key={item.reason} title={item.reason}><span>{item.reason}</span><strong>{item.count.toLocaleString()}</strong></div>)}</div>:<div className="empty-hint">No failed callback jobs to summarize.</div>}
        </div>
      </section>
      <section className="panel monitor-panel"><div className="section-title"><div><h3>Recent callback deliveries</h3><p>Latest jobs submitted by async scenarios.</p></div></div>
        {jobs.length?jobs.slice(0,8).map(job=><div className="monitor-job" key={job.id}><div><strong className={`job-state state-${job.state.toLowerCase()}`}>{job.state}</strong><span>{job.scenario_key} · v{job.scenario_version}</span></div><small title={job.correlation_id}>{job.correlation_id}</small></div>):<div className="empty-hint">No callback jobs recorded yet.</div>}
      </section>
    </div>
    <div className="monitor-footnote">Throughput counts successful simulator responses over a rolling window. CPU and memory show the API and callback worker process usage.</div>
  </section>
}

export default function App() {
  const [items,setItems] = useState<ScenarioSummary[]>([])
  const [scenarioSearch,setScenarioSearch] = useState('')
  const [scenarioOffset,setScenarioOffset] = useState(0)
  const [scenarioTotal,setScenarioTotal] = useState(0)
  const [scenarioRefresh,setScenarioRefresh] = useState(0)
  const [scenariosLoading,setScenariosLoading] = useState(false)
  const [scenarioListError,setScenarioListError] = useState('')
  const [loadingScenarioKey,setLoadingScenarioKey] = useState('')
  const [deletingScenarioKey,setDeletingScenarioKey] = useState('')
  const [authRequired,setAuthRequired] = useState(false)
  const [apiKeyDraft,setApiKeyDraft] = useState(()=>sessionStorage.getItem('usim-api-key')||'')
  const [form,setForm] = useState<ScenarioForm>(newForm())
  const [jobs,setJobs] = useState<CallbackJob[]>([])
  const [message,setMessage] = useState('')
  const [busy,setBusy] = useState(false)
  const [monitoring,setMonitoring] = useState(false)
  const [metrics,setMetrics] = useState<SimulatorMetrics|null>(null)
  const [metricsError,setMetricsError] = useState('')
  const [callbackActionBusy,setCallbackActionBusy] = useState(false)
  const [callbackActionMessage,setCallbackActionMessage] = useState('')
  const captureNames = useMemo(()=>form.captures.filter(item=>item.name.trim()).map(item=>item.name),[form.captures])
  const parsedRequest=useMemo(()=>parseSample(form.requestFormat,form.requestSample),[form.requestFormat,form.requestSample])
  const requestPathParameters=useMemo(()=>pathParameterNames(form.pathTemplate),[form.pathTemplate])

  useEffect(()=>{
    const showKeyPrompt=()=>setAuthRequired(true)
    window.addEventListener('simulator-auth-required',showKeyPrompt)
    return()=>window.removeEventListener('simulator-auth-required',showKeyPrompt)
  },[])

  function changeRequestSample(sample:string) {
    setForm(current=>{
      const parsed=parseSample(current.requestFormat,sample)
      let namespaces=current.namespaces
      if(current.requestFormat==='xml'&&!parsed.error&&Object.keys(parsed.namespaces).length){
        try { namespaces=JSON.stringify({...JSON.parse(current.namespaces||'{}'),...parsed.namespaces},null,2) }
        catch { namespaces=JSON.stringify(parsed.namespaces,null,2) }
      }
      return {...current,requestSample:sample,namespaces}
    })
  }

  async function refresh() {
    try {
      const jobsResponse=await apiFetch(`${API}/api/callback-jobs`)
      if (jobsResponse.ok) setJobs(await jobsResponse.json())
    } catch { setMessage('Cannot reach the simulator API. Check that the Docker services are running.') }
  }
  useEffect(()=>{refresh();const timer=setInterval(refresh,5000);return()=>clearInterval(timer)},[])

  useEffect(()=>{
    if(monitoring)return
    const controller=new AbortController()
    const timer=setTimeout(async()=>{
      setScenariosLoading(true);setScenarioListError('')
      try {
        const params=new URLSearchParams({search:scenarioSearch.trim(),offset:String(scenarioOffset),limit:'50'})
        const response=await apiFetch(`${API}/api/scenarios?${params}`,{signal:controller.signal})
        if(!response.ok)throw new Error(`Scenario search returned HTTP ${response.status}`)
        const result=await response.json()
        setItems(result.items);setScenarioTotal(result.total)
      } catch(error) {
        if(!controller.signal.aborted)setScenarioListError(error instanceof Error?error.message:'Could not load scenarios.')
      } finally {if(!controller.signal.aborted)setScenariosLoading(false)}
    },220)
    return()=>{clearTimeout(timer);controller.abort()}
  },[monitoring,scenarioSearch,scenarioOffset,scenarioRefresh])

  async function refreshMetrics() {
    try {
      const response=await apiFetch(`${API}/api/metrics`)
      if (!response.ok) throw new Error(`Metrics endpoint returned HTTP ${response.status}`)
      setMetrics(await response.json())
      setMetricsError('')
    } catch (error) { setMetricsError(error instanceof Error?error.message:'Could not load simulator metrics.') }
  }
  useEffect(()=>{if(!monitoring)return;refreshMetrics();const timer=setInterval(refreshMetrics,2000);return()=>clearInterval(timer)},[monitoring])

  async function callbackWorkerAction(action:'stop-and-clear'|'resume') {
    if(action==='stop-and-clear'&&!window.confirm('Pause callback delivery and permanently clear all pending, in-progress, succeeded, and failed callback records? A callback already in flight may finish sending.'))return
    setCallbackActionBusy(true);setCallbackActionMessage('')
    try {
      const response=await apiFetch(`${API}/api/callback-worker/${action}`,{method:'POST'})
      const result=await response.json()
      if(!response.ok)throw new Error(result.detail||`Action failed (HTTP ${response.status})`)
      setCallbackActionMessage(action==='stop-and-clear'?`Worker paused. Cleared ${Number(result.deleted_jobs||0).toLocaleString()} callback records.`:'Callback worker resumed.')
      await Promise.all([refreshMetrics(),refresh()])
    } catch(error) { setCallbackActionMessage(error instanceof Error?error.message:'Callback worker action failed.') }
    finally {setCallbackActionBusy(false)}
  }

  async function validateOrSave(save:boolean) {
    setBusy(true)
    try {
      const definition=definitionFromForm(form)
      const response=save
        ? await apiFetch(`${API}/api/scenarios/${encodeURIComponent(definition.key)}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(definition)})
        : await apiFetch(`${API}/api/scenarios/validate`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(definition)})
      const result=await response.json()
      if (!response.ok) throw new Error(Array.isArray(result.detail)?result.detail.map((item:any)=>`${item.loc?.join('.')}: ${item.msg}`).join('\n'):String(result.detail || 'Request failed'))
      setMessage(save?`Saved ${result.key} version ${result.version}.`:`${result.key} is valid and ready to save.`)
      if (save) {refresh();setScenarioRefresh(value=>value+1)}
    } catch (error) { setMessage(error instanceof Error?error.message:'Could not save scenario.') }
    finally { setBusy(false) }
  }

  const setCapture=(index:number,patch:Partial<Capture>)=>setForm(current=>({...current,captures:current.captures.map((capture,i)=>i===index?{...capture,...patch}:capture)}))
  async function loadScenario(scenario:ScenarioSummary) {
    setLoadingScenarioKey(scenario.key)
    try {
      const response=await apiFetch(`${API}/api/scenarios/${encodeURIComponent(scenario.key)}`)
      const result=await response.json()
      if(!response.ok)throw new Error(result.detail||`Could not load scenario (HTTP ${response.status})`)
      setForm(formFromDefinition(result.definition));setMessage(`Editing ${scenario.key} version ${result.version}. Saving creates a new version.`)
    } catch(error) {setMessage(error instanceof Error?error.message:'This scenario definition could not be loaded into the form.')}
    finally {setLoadingScenarioKey('')}
  }

  async function deleteScenario(scenario:ScenarioSummary) {
    if(!window.confirm(`Delete scenario “${scenario.name}” (${scenario.key}) and its saved version history? This cannot be undone.`))return
    setDeletingScenarioKey(scenario.key);setScenarioListError('')
    try {
      const response=await apiFetch(`${API}/api/scenarios/${encodeURIComponent(scenario.key)}`,{method:'DELETE'})
      if(!response.ok){
        let detail=`Could not delete scenario (HTTP ${response.status})`
        try {const result=await response.json();detail=String(result.detail||detail)} catch {}
        throw new Error(detail)
      }
      if(form.key===scenario.key){setForm(newForm());setMessage(`Deleted ${scenario.key}.`)}
      setItems(current=>current.filter(item=>item.key!==scenario.key));setScenarioTotal(current=>Math.max(0,current-1))
      setScenarioOffset(current=>current>=Math.max(0,scenarioTotal-1)?Math.max(0,current-50):current)
      setScenarioRefresh(value=>value+1)
    } catch(error) {setScenarioListError(error instanceof Error?error.message:'Could not delete scenario.')}
    finally {setDeletingScenarioKey('')}
  }

  return <main>
    <header><div><div className="eyebrow">BSS TEST TOOLING</div><h1>Integration Simulator</h1><p>Configure partner behavior, exercise sync and ACK-then-callback flows.</p></div><div className="header-actions">{authRequired&&<form className="auth-key-control" onSubmit={event=>{event.preventDefault();sessionStorage.setItem('usim-api-key',apiKeyDraft.trim());setAuthRequired(false);setScenarioRefresh(value=>value+1);refresh();refreshMetrics()}}><input type="password" autoComplete="current-password" value={apiKeyDraft} onChange={event=>setApiKeyDraft(event.target.value)} placeholder="Simulator API key" aria-label="Simulator API key"/><button type="submit">Connect</button></form>}<button onClick={()=>setMonitoring(value=>!value)}>{monitoring?'Scenario builder':'Live monitoring'}</button>{!monitoring&&<button className="primary" onClick={()=>{setForm(newForm());setMessage('New scenario form is ready.')}}>New scenario</button>}</div></header>
    {monitoring?<MonitoringDashboard metrics={metrics} error={metricsError} jobs={jobs} onRefresh={refreshMetrics} onStopAndClear={()=>callbackWorkerAction('stop-and-clear')} onResume={()=>callbackWorkerAction('resume')} actionBusy={callbackActionBusy} actionMessage={callbackActionMessage}/>:<section className="layout">
      <aside className="panel sidebar"><div className="section-title"><div><h2>Scenarios</h2><p>{scenarioTotal.toLocaleString()} saved simulator behaviors</p></div><button onClick={()=>setScenarioRefresh(value=>value+1)}>Refresh</button></div>
        <label className="scenario-search-label">Search scenarios<input className="scenario-search" type="search" value={scenarioSearch} onChange={event=>{setScenarioSearch(event.target.value);setScenarioOffset(0)}} placeholder="Search by name or key"/></label>
        {scenarioListError&&<div className="scenario-list-error">{scenarioListError}</div>}
        {scenariosLoading&&<p className="muted empty-scenarios">Searching scenarios…</p>}
        {!scenariosLoading&&items.length===0&&<p className="muted empty-scenarios">{scenarioSearch.trim()?'No matching scenarios.':'No scenarios yet. Create one with the form.'}</p>}
        {items.map(item=><div className={`scenario-row ${item.key===form.key?'selected':''}`} key={item.key}>
          <button className="scenario" onClick={()=>loadScenario(item)} disabled={Boolean(loadingScenarioKey)||Boolean(deletingScenarioKey)} aria-label={`Edit ${item.name}`}><strong>{item.name}</strong><span>{loadingScenarioKey===item.key?'Loading…':`${item.key} · v${item.version}`}</span><em>{item.flow.toUpperCase()}</em></button>
          <button type="button" className="scenario-delete" title={`Delete ${item.name}`} aria-label={`Delete ${item.name}`} onClick={()=>deleteScenario(item)} disabled={Boolean(deletingScenarioKey)||Boolean(loadingScenarioKey)}>{deletingScenarioKey===item.key?'Deleting…':'Delete'}</button>
        </div>)}
        {scenarioTotal>50&&<div className="scenario-pagination"><button onClick={()=>setScenarioOffset(Math.max(0,scenarioOffset-50))} disabled={scenarioOffset===0}>Previous</button><span>{scenarioOffset+1}–{Math.min(scenarioOffset+items.length,scenarioTotal)} of {scenarioTotal.toLocaleString()}</span><button onClick={()=>setScenarioOffset(scenarioOffset+50)} disabled={scenarioOffset+50>=scenarioTotal}>Next</button></div>}
      </aside>
      <section className="panel editor">
        <div className="editor-heading"><div><div className="eyebrow">SCENARIO BUILDER</div><h2>{form.name||'New scenario'}</h2><p>Set up the request, then define the response values the partner simulator should return.</p></div><span className="badge">{form.flow==='sync'?'SYNCHRONOUS':'ACK + CALLBACK'}</span></div>
        <section className="step-section"><div className="step-title"><span>1</span><div><h3>Scenario and request</h3><p>How the BSS reaches this simulated partner operation.</p></div></div>
          <div className="form-grid">
            <label>Scenario key <span className="required">Required</span><input value={form.key} onChange={e=>setForm({...form,key:e.target.value})} placeholder="partner-key-provisioning"/><small>Used in the simulator URL: /sim/&lt;scenario-key&gt;</small></label>
            <label>Display name<input value={form.name} onChange={e=>setForm({...form,name:e.target.value})} placeholder="Partner subscriber key provisioning"/></label>
            <label>Request method<select value={form.method} onChange={e=>setForm({...form,method:e.target.value})}>{['POST','GET','PUT','PATCH','DELETE'].map(method=><option key={method}>{method}</option>)}</select></label>
            <label>Request format<select value={form.requestFormat} onChange={e=>setForm({...form,requestFormat:e.target.value as Format,requestSample:''})}><option value="xml">XML / SOAP</option><option value="json">JSON</option></select></label>
            <label>Flow type<select value={form.flow} onChange={e=>setForm({...form,flow:e.target.value as Flow})}><option value="sync">Synchronous response</option><option value="async">ACK then callback</option></select><small>Choose async only when the partner calls back later with the final result.</small></label>
            <label className="wide">URL path template <span className="optional">Optional</span><input value={form.pathTemplate} onChange={e=>setForm({...form,pathTemplate:e.target.value})} placeholder="/subscriber/{msisdn}"/><small>Add the path after the scenario key. Use {`{parameterName}`} for a variable path segment, such as /subscriber/{`{msisdn}`}.</small></label>
          </div>
          <div className="endpoint-preview"><span>Configure the BSS endpoint as</span><code>{API}/sim/{form.key||'scenario-key'}{form.pathTemplate?`/${form.pathTemplate.replace(/^\/+|\/+$/g,'')}`:''}</code></div>
          <div className="sample-card request-sample"><div className="table-heading"><div><h4>Sample request body</h4><p>Paste a representative {form.requestFormat.toUpperCase()} request. Its fields become selectable capture paths below.</p></div><button type="button" onClick={()=>changeRequestSample('')} disabled={!form.requestSample}>Clear sample</button></div><textarea className="payload-editor sample-editor" spellCheck={false} value={form.requestSample} onChange={e=>changeRequestSample(e.target.value)} placeholder={form.requestFormat==='json'?'Paste a JSON request body here':'Paste an XML / SOAP request body here'}/><div className={`sample-status ${parsedRequest.error?'sample-error':''}`}>{parsedRequest.error?parsedRequest.error:`${parsedRequest.fields.length} selectable fields detected`}</div></div>
        </section>

        <section className="step-section"><div className="step-title"><span>2</span><div><h3>Request captures <span className="optional">Optional</span></h3><p>Capture request values that should be copied into a response or used as its correlation ID.</p></div><button type="button" onClick={()=>setForm({...form,captures:[...form.captures,{name:'',source:form.requestFormat,path:''}]})}>+ Add capture</button></div>
          {form.captures.length===0?<div className="empty-hint">No request fields need to be captured. The simulator will return the configured response for requests sent to this endpoint.</div>:form.captures.map((capture,index)=><div className="capture-row" key={index}>
            <label>Capture name<input value={capture.name} placeholder="transactionId" onChange={e=>setCapture(index,{name:e.target.value})}/></label>
            <label>Read from<select value={capture.source} onChange={e=>setCapture(index,{source:e.target.value as CaptureSource})}><option value="xml">XML / SOAP body</option><option value="json">JSON body</option><option value="header">HTTP header</option><option value="query">Query parameter</option><option value="path">URL path parameter</option></select></label>
            <label className="capture-path">{capture.source==='json'||capture.source==='xml'?'Field from sample':capture.source==='path'?'URL path parameter':'Header or query name'}{capture.source==='json'?<JsonCapturePicker source={form.requestSample} path={capture.path} onSelect={path=>setCapture(index,{path,name:capture.name.trim()?capture.name:safeToken(path)})}/>:capture.source==='xml'?<select value={capture.path} onChange={e=>{const path=e.target.value;setCapture(index,{path,name:capture.name.trim()?capture.name:safeToken(path)})}}><option value="">Choose a parsed field</option>{parsedRequest.fields.map(field=><option key={field.path} value={field.path}>{field.label} · {String(field.value).slice(0,48)}</option>)}{capture.path&&!parsedRequest.fields.some(field=>field.path===capture.path)&&<option value={capture.path}>{capture.path} · from saved config</option>}</select>:capture.source==='path'?<select value={capture.path} onChange={e=>{const path=e.target.value;setCapture(index,{path,name:capture.name.trim()?capture.name:safeToken(path)})}}><option value="">Choose a path parameter</option>{requestPathParameters.map(name=><option key={name} value={name}>{name}</option>)}{capture.path&&!requestPathParameters.includes(capture.path)&&<option value={capture.path}>{capture.path} · from saved config</option>}</select>:<input value={capture.path} placeholder={capture.source==='header'?'X-Transaction-ID':'transactionId'} onChange={e=>setCapture(index,{path:e.target.value})}/>}</label>
            <button type="button" className="icon-button danger" title="Remove capture" onClick={()=>setForm({...form,captures:form.captures.filter((_,i)=>i!==index)})}>×</button>
          </div>)}
          {form.captures.some(capture=>capture.source==='xml')&&<label className="namespace-field">XML namespaces <small>Automatically filled from the pasted XML sample; edit only if your XML needs additional prefixes.</small><textarea className="small-code" value={form.namespaces} onChange={e=>setForm({...form,namespaces:e.target.value})}/></label>}
        </section>

        {form.flow==='sync'?<section className="step-section"><div className="step-title"><span>3</span><div><h3>Synchronous response</h3><p>Configure the response returned immediately to the BSS.</p></div></div><ReplyEditor title="Final response" reply={form.response} captures={form.captures} onChange={response=>setForm({...form,response})}/></section>:<section className="step-section"><div className="step-title"><span>3</span><div><h3>Async acknowledgement and callback</h3><p>Return an acknowledgement first, then deliver the configured final response to the BSS callback.</p></div></div>
          <div className="form-grid async-grid">
            <label>Callback URL <span className="required">Required</span><input value={form.callbackUrl} onChange={e=>setForm({...form,callbackUrl:e.target.value})} placeholder="https://bss.example/callback"/><small>Must be allowed by CALLBACK_ALLOWED_HOSTS in the backend deployment.</small></label>
            <label>Callback delay (ms)<input type="number" min="1" value={form.delayMs} onChange={e=>setForm({...form,delayMs:Number(e.target.value)})}/></label>
            <label>Correlation capture<select value={form.correlationCapture} onChange={e=>setForm({...form,correlationCapture:e.target.value})}><option value="">Choose captured request value</option>{captureNames.map(name=><option key={name}>{name}</option>)}</select></label>
            <label>Maximum delivery attempts<input type="number" min="1" max="20" value={form.maxAttempts} onChange={e=>setForm({...form,maxAttempts:Number(e.target.value)})}/></label>
            <label>Retry delay (ms)<input type="number" min="0" value={form.retryDelayMs} onChange={e=>setForm({...form,retryDelayMs:Number(e.target.value)})}/></label>
          </div>
          <ReplyEditor title="Immediate acknowledgement" reply={form.ack} captures={form.captures} onChange={ack=>setForm({...form,ack})}/>
          <ReplyEditor title="Final callback" reply={form.callback} captures={form.captures} onChange={callback=>setForm({...form,callback})}/>
        </section>}
        <div className="save-bar"><div role="status" className="status-message">{message||'Save the scenario to make it available to the BSS.'}</div><button onClick={()=>validateOrSave(false)} disabled={busy}>{busy?'Working…':'Validate'}</button><button className="primary" onClick={()=>validateOrSave(true)} disabled={busy}>{busy?'Saving…':'Save scenario'}</button></div>
      </section>
    </section>}
    {!monitoring&&<footer className="app-footer">Simulator traffic is available at <code>{API}/sim/&lt;scenario-key&gt;</code>. Only configure callback hosts approved for your test environment.</footer>}
  </main>
}
