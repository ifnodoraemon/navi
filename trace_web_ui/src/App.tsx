import { useEffect, useState } from 'react';
import axios from 'axios';
import { format } from 'date-fns';
import { Activity, Code, CheckCircle2, XCircle, Search, Clock, ChevronDown, ChevronRight, Zap, Copy, Check, RefreshCw, Timer, ShieldAlert, Layers, Inbox, Send, Download, Play, Pause, Trash2, RotateCcw, Rocket, ListTree, MessageSquare, Database } from 'lucide-react';
import { JsonView } from 'react-json-view-lite';
import 'react-json-view-lite/dist/index.css';
import ReactMarkdown from 'react-markdown';
import type { TraceData, TraceMeta, TraceRunView } from './types';
import './App.css';


const CollapsibleJson = ({ title, jsonStr, defaultOpen = false }: { title: string, jsonStr: string | object | null, defaultOpen?: boolean }) => {
  const [isOpen, setIsOpen] = useState(defaultOpen);
  const [copied, setCopied] = useState(false);

  if (jsonStr === null || jsonStr === undefined || jsonStr === '' || jsonStr === '{}') return null;

  const customJsonStyle = {
    container: 'jv-container',
    basicChildStyle: 'jv-child',
    label: 'jv-label',
    nullValue: 'jv-null',
    undefinedValue: 'jv-undefined',
    stringValue: 'jv-string',
    booleanValue: 'jv-boolean',
    numberValue: 'jv-number',
    otherValue: 'jv-other',
    punctuation: 'jv-punctuation',
    collapseIcon: 'jv-icon',
    expandIcon: 'jv-icon',
    collapsedContent: 'jv-collapsed',
  };

  const renderJson = () => {
    let parsed;
    try {
      parsed = typeof jsonStr === 'string' ? JSON.parse(jsonStr) : jsonStr;
    } catch (e) {
      parsed = jsonStr;
    }

    if (typeof parsed !== 'object' || parsed === null) {
      parsed = { payload: parsed };
    }

    return <JsonView data={parsed} shouldExpandNode={(level) => level < 2} style={customJsonStyle} />;
  };

  const handleCopy = (e: React.MouseEvent) => {
    e.stopPropagation();
    let textToCopy = String(jsonStr);
    try {
      if (typeof jsonStr === 'string') {
        textToCopy = JSON.stringify(JSON.parse(jsonStr), null, 2);
      } else {
        textToCopy = JSON.stringify(jsonStr, null, 2);
      }
    } catch (e) {}
    navigator.clipboard.writeText(textToCopy);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const handleCopyRaw = (e: React.MouseEvent) => {
    e.stopPropagation();
    navigator.clipboard.writeText(typeof jsonStr === 'string' ? jsonStr : JSON.stringify(jsonStr, null, 2));
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="data-section">
      <div className="data-title" onClick={(e) => { e.stopPropagation(); setIsOpen(!isOpen); }}>
        <div style={{ display: 'flex', alignItems: 'center' }}>
          {isOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
          <Code size={14} style={{ marginRight: 6, marginLeft: 4 }} />
          <span>{title}</span>
        </div>
        {isOpen && (
          <div style={{ display: 'flex', gap: 6 }}>
            <button className="copy-btn raw-btn" onClick={handleCopyRaw} title="Copy exact raw text">
              {copied ? <Check size={14} color="var(--success-color)" /> : <Database size={14} />} Raw
            </button>
            <button className="copy-btn" onClick={handleCopy} title="Copy to clipboard">
              {copied ? <Check size={14} color="var(--success-color)" /> : <Copy size={14} />}
            </button>
          </div>
        )}
      </div>
      {isOpen && (
        <div className="code-container glass-panel" onClick={e => e.stopPropagation()}>
          {renderJson()}
        </div>
      )}
    </div>
  );
};

const RunNode = ({
  run,
  allRuns,
  traceTotalDuration,
  firstTime,
  depth = 0,
  autoExpand = false,
  showLLM = true,
  showTool = true,
  showEngine = true
}: {
  run: TraceRunView,
  allRuns: TraceRunView[],
  traceTotalDuration: number,
  firstTime: number,
  depth?: number,
  autoExpand?: boolean,
  showLLM?: boolean,
  showTool?: boolean,
  showEngine?: boolean
}) => {
  const [expanded, setExpanded] = useState(depth < 2 || autoExpand);

  const children = allRuns.filter(r => r.parent_run_id === run.id).sort((a,b) => a.start_time - b.start_time);
  const hasChildren = children.length > 0;

  const eventStart = Math.max(0, run.start_time - firstTime);
  const eventDuration = Math.max(0, run.end_time - run.start_time);
  const leftPercent = traceTotalDuration > 0 ? (eventStart / traceTotalDuration) * 100 : 0;
  const widthPercent = traceTotalDuration > 0 ? Math.max(0.5, (eventDuration / traceTotalDuration) * 100) : 100;

  const isLLM = run.run_type === 'llm';
  const isTool = run.run_type === 'tool';
  const isEngine = !isLLM && !isTool && run.name !== 'Trace';
  const isVisible = (isLLM && showLLM) || (isTool && showTool) || (isEngine && showEngine) || (!isLLM && !isTool && !isEngine);

  const childrenContent = hasChildren ? (
    <div className="run-children-list">
      {children.map(child => (
        <RunNode
          key={child.id}
          run={child}
          allRuns={allRuns}
          traceTotalDuration={traceTotalDuration}
          firstTime={firstTime}
          depth={depth + 1}
          autoExpand={autoExpand}
          showLLM={showLLM}
          showTool={showTool}
          showEngine={showEngine}
        />
      ))}
    </div>
  ) : null;

  if (!isVisible) {
    return <>{childrenContent}</>;
  }

  const isError = run.status === 'error';
  const isBlocked = run.status === 'blocked';

  const statusClass = isError ? 'error' : isBlocked ? 'blocked' : 'success';
  let StatusIcon = isError ? XCircle : isBlocked ? ShieldAlert : CheckCircle2;
  if (run.name === 'Channel Receive') StatusIcon = Inbox;
  if (run.name === 'Channel Send') StatusIcon = Send;
  if (run.name === 'Turn') StatusIcon = Layers;

  // Custom renders based on run_type
  let runContent = null;
  if (!hasChildren && expanded) {
    if (run.run_type === 'llm') {
      const inputs = run.inputs || {};
      const outputs = run.outputs || {};
      const prompt = inputs.message || inputs.prompt || inputs.system_prompt || inputs;
      const rawPromptText = typeof prompt === 'string' ? prompt : JSON.stringify(prompt, null, 2);

      let completionStr = "";
      if (outputs?.generations?.[0]?.message?.content) {
         completionStr = outputs.generations[0].message.content;
      } else if (outputs.text || outputs.message) {
         completionStr = outputs.text || outputs.message;
      }

      runContent = (
        <div className="run-details">
          {prompt && (
            <div className="message-box markdown-body" style={{ background: 'rgba(255,255,255,0.03)', padding: '12px', borderRadius: '6px', marginBottom: '8px', borderLeft: '3px solid var(--text-secondary)' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 4 }}>
                <span style={{ fontSize: '0.75rem', opacity: 0.6, textTransform: 'uppercase' }}>Prompt</span>
                <button className="copy-btn raw-btn" onClick={(e) => { e.stopPropagation(); navigator.clipboard.writeText(rawPromptText); alert("Prompt copied to clipboard!"); }} title="Copy exact raw text">
                  <Database size={14} /> Copy Raw Prompt
                </button>
              </div>
              <ReactMarkdown>{typeof prompt === 'string' ? prompt : JSON.stringify(prompt)}</ReactMarkdown>
            </div>
          )}
          {completionStr ? (
             <div className="message-box markdown-body" style={{ background: 'rgba(0,0,0,0.3)', padding: '12px', borderRadius: '6px', borderLeft: '3px solid var(--accent-color)' }}>
               <div style={{ fontSize: '0.75rem', opacity: 0.6, marginBottom: 4, textTransform: 'uppercase' }}>Completion</div>
               <ReactMarkdown>{completionStr}</ReactMarkdown>
             </div>
          ) : (
             <CollapsibleJson title="LLM Output" jsonStr={outputs} defaultOpen={true} />
          )}
        </div>
      );
    } else if (run.run_type === 'tool') {
      const args = run.inputs?.args || run.inputs;
      const result = run.outputs;
      runContent = (
        <div className="run-details">
          <CollapsibleJson title="Arguments" jsonStr={args} defaultOpen={true} />
          <CollapsibleJson title="Result" jsonStr={result} defaultOpen={isError} />
        </div>
      );
    } else {
      runContent = (
        <div className="run-details">
          <CollapsibleJson title="Inputs" jsonStr={run.inputs} />
          <CollapsibleJson title="Outputs" jsonStr={run.outputs} />
        </div>
      );
    }
  }

  const formatDuration = (sec: number) => {
    if (sec < 1) return `${Math.round(sec * 1000)}ms`;
    return `${sec.toFixed(2)}s`;
  };

  let tokenDisplay = null;
  if (run.run_type === 'llm') {
    const usage = run.outputs?.usage || run.inputs?.usage;
    if (usage && usage.total_tokens) {
      tokenDisplay = (
        <span style={{ marginLeft: 8, padding: '2px 6px', background: 'rgba(59, 130, 246, 0.2)', color: '#60a5fa', borderRadius: 4, fontSize: '0.65rem', border: '1px solid rgba(59,130,246,0.3)' }}>
          {usage.total_tokens} tokens
        </span>
      );
    }
  }

  return (
    <div className={`run-node depth-${depth} ${expanded ? 'expanded' : 'collapsed'}`}>
      <div
        className={`run-header glass-panel ${statusClass}`}
        onClick={() => setExpanded(!expanded)}
      >
        <div className="run-header-content">
           <div className="run-toggle">
              {hasChildren ? (expanded ? <ChevronDown size={16} /> : <ChevronRight size={16} />) : <span style={{width: 16, display: 'inline-block'}}></span>}
           </div>
           <div className={`run-icon ${statusClass}`}>
              <StatusIcon size={16} />
           </div>
           <div className="run-title-area">
              <div className="run-name">
                 <span className={`run-type-badge type-${run.run_type}`}>{run.run_type}</span>
                 {run.name}
                 {tokenDisplay}
              </div>
              <div className="run-meta">
                 <span>{format(new Date(run.start_time * 1000), 'HH:mm:ss.SSS')}</span>
                 <span className="separator">•</span>
                 <span style={{ fontWeight: 600, color: 'var(--text-primary)' }}>{formatDuration(eventDuration)}</span>
              </div>
           </div>
        </div>

        <div className="run-waterfall-bg">
          <div
             className={`run-waterfall-bar ${statusClass}`}
             style={{
               left: `${leftPercent}%`,
               width: `${widthPercent}%`
             }}
          />
        </div>
      </div>

      {expanded && (
        <div className="run-body">
          {runContent}
          {childrenContent}
        </div>
      )}
    </div>
  );
};


function App() {
  const [tracesMeta, setTracesMeta] = useState<TraceMeta[]>([]);
  const [selectedTrace, setSelectedTrace] = useState<string | null>(null);
  const [traceData, setTraceData] = useState<TraceData | null>(null);
  const [loading, setLoading] = useState(false);
  const [isRefreshing, setIsRefreshing] = useState(false);

  // List Filters
  const [listHasError, setListHasError] = useState(false);
  const [listShowDaemon, setListShowDaemon] = useState(false);
  const [listLimit, setListLimit] = useState(50);

  // Filters & Search
  const [showErrorsOnly, setShowErrorsOnly] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  
  // Phase 3 Titan State
  const [globalSearch, setGlobalSearch] = useState('');
  const [viewMode, setViewMode] = useState<'tree' | 'chat'>('tree');
  const [filterLLM, setFilterLLM] = useState(true);
  const [filterTool, setFilterTool] = useState(true);
  const [filterEngine, setFilterEngine] = useState(true);

  // Live Mode
  const [autoRefresh, setAutoRefresh] = useState(false);

  // Deep Linking URL Hash
  useEffect(() => {
    const handleHash = () => {
      const hash = window.location.hash.replace('#', '');
      if (hash && hash !== selectedTrace) {
        loadTrace(hash);
      }
    };
    handleHash();
    window.addEventListener('hashchange', handleHash);
    return () => window.removeEventListener('hashchange', handleHash);
  }, []);

  useEffect(() => {
    if (selectedTrace) {
      window.location.hash = selectedTrace;
    } else {
      window.location.hash = '';
    }
  }, [selectedTrace]);

  useEffect(() => {
    fetchTraceIds();
    const interval = setInterval(() => {
      if (autoRefresh) {
        fetchTraceIds(false);
      }
    }, 5000);
    return () => clearInterval(interval);
  }, [listHasError, listShowDaemon, listLimit, autoRefresh, globalSearch]);

  // If autoRefresh is on and we have a selectedTrace, poll it
  useEffect(() => {
    if (!autoRefresh || !selectedTrace) return;
    const interval = setInterval(() => {
      axios.get(`/v1/traces/${selectedTrace}`).then(res => {
        const rawData = res.data;
        const actualData = (rawData && rawData.data && rawData.data.events) ? rawData.data : rawData;
        setTraceData(actualData);
      }).catch(err => console.error('Failed to auto-refresh trace', err));
    }, 5000);
    return () => clearInterval(interval);
  }, [autoRefresh, selectedTrace]);

  const downloadTrace = () => {
    if (!traceData || !selectedTrace) return;
    const blob = new Blob([JSON.stringify(traceData, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `trace_${selectedTrace}.json`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  const fetchTraceIds = async (showRefresh = false) => {
    if (showRefresh) setIsRefreshing(true);
    try {
      const params: any = { limit: listLimit };
      if (listHasError) {
        params.has_error = true;
      }
      if (globalSearch) {
        params.query = globalSearch;
      }
      const res = await axios.get('/v1/traces', { params });
      const rawData = res.data;
      const actualData = (rawData && rawData.data && rawData.data.traces) ? rawData.data : rawData;
      const meta = actualData.traces || [];
      const filteredMeta = meta.filter((t: TraceMeta) => listShowDaemon || !t.trace_id.startsWith('daemon-trace-'));
      const sortedMeta = [...filteredMeta].sort((a: TraceMeta, b: TraceMeta) => b.start_time - a.start_time);
      setTracesMeta(sortedMeta);
    } catch (err) {
      console.error('Failed to fetch traces', err);
    } finally {
      if (showRefresh) {
        setTimeout(() => setIsRefreshing(false), 500);
      }
    }
  };

  const handleDeleteAll = async () => {
    if (!confirm("Are you sure you want to clear ALL traces? This cannot be undone.")) return;
    try {
      await axios.delete('/v1/traces');
      setTracesMeta([]);
      setSelectedTrace(null);
      setTraceData(null);
    } catch (err) {
      alert("Failed to delete traces.");
    }
  };

  const handleDeleteTrace = async () => {
    if (!selectedTrace) return;
    if (!confirm("Delete this trace?")) return;
    try {
      await axios.delete(`/v1/traces/${selectedTrace}`);
      fetchTraceIds(true);
      setSelectedTrace(null);
      setTraceData(null);
    } catch (err) {
      alert("Failed to delete trace.");
    }
  };

  const loadTrace = async (id: string) => {
    setSelectedTrace(id);
    setLoading(true);
    setTraceData(null);
    try {
      const res = await axios.get(`/v1/traces/${id}`);
      const rawData = res.data;
      const actualData = (rawData && rawData.data && rawData.data.events) ? rawData.data : rawData;
      setTraceData(actualData);
    } catch (err) {
      console.error('Failed to load trace', err);
    } finally {
      setLoading(false);
    }
  };

  let totalDuration = 0;
  let rootRuns: TraceRunView[] = [];
  let allRuns: TraceRunView[] = [];
  let firstTime = 0;
  let totalTokensInput = 0;
  let totalTokensOutput = 0;

  if (traceData && traceData.runs && traceData.runs.length > 0) {
    allRuns = traceData.runs;

    // Apply filters
    if (searchQuery.trim() !== '' || showErrorsOnly) {
       const q = searchQuery.toLowerCase();
       const matches = new Set<string>();

       const nodeMatches = (r: TraceRunView) => {
         if (showErrorsOnly && r.status !== 'error') return false;
         if (q === '') return true;
         return (
           r.name.toLowerCase().includes(q) ||
           r.run_type.toLowerCase().includes(q) ||
           JSON.stringify(r.inputs).toLowerCase().includes(q) ||
           JSON.stringify(r.outputs).toLowerCase().includes(q)
         );
       };

       allRuns.forEach(r => {
         if (nodeMatches(r)) {
           matches.add(r.id);
           let current = r;
           while (current.parent_run_id) {
             const parent = allRuns.find(p => p.id === current.parent_run_id);
             if (parent) {
               matches.add(parent.id);
               current = parent;
             } else {
               break;
             }
           }
         }
       });

       allRuns = allRuns.filter(r => matches.has(r.id));
    }

    // Find roots
    rootRuns = allRuns.filter(r => !r.parent_run_id || r.parent_run_id === '').sort((a,b) => a.start_time - b.start_time);

    // Fallback if roots aren't correctly marked
    if (rootRuns.length === 0 && allRuns.length > 0) {
       // Find the earliest run that is not a child of anything in the set
       const runIds = new Set(allRuns.map(r => r.id));
       rootRuns = allRuns.filter(r => !runIds.has(r.parent_run_id || ''));
    }

    if (allRuns.length > 0) {
      firstTime = Math.min(...allRuns.map(r => r.start_time));
      const lastTime = Math.max(...allRuns.map(r => r.end_time));
      totalDuration = lastTime - firstTime;
    }
  }

  const errorPaths = new Set<string>();
  if (traceData?.runs) {
    const expandPath = (runId: string) => {
      errorPaths.add(runId);
      const run = traceData.runs.find(r => r.id === runId);
      if (run && run.parent_run_id) expandPath(run.parent_run_id);
    };
    traceData.runs.forEach(r => {
      if (r.status === 'error') expandPath(r.id);
      if (r.run_type === 'llm' && r.outputs?.usage) {
         totalTokensInput += r.outputs.usage.prompt_tokens || 0;
         totalTokensOutput += r.outputs.usage.completion_tokens || 0;
      }
    });
  }

  const estimatedCost = (totalTokensInput * 0.005 / 1000) + (totalTokensOutput * 0.015 / 1000);

  const handleReplay = async () => {
    const firstUserMsg = allRuns.find(r => r.name === 'Channel Receive')?.inputs?.message;
    const replayText = typeof firstUserMsg === 'string' ? firstUserMsg : firstUserMsg?.text || JSON.stringify(firstUserMsg);
    if (!replayText) return alert("Could not extract original User input from Channel Receive.");
    if (!confirm(`Replay original input?\n\n"${replayText}"`)) return;
    try {
      await axios.post('/v1/chat', { message: replayText });
      setTimeout(() => fetchTraceIds(true), 2000);
      alert("Replay request sent!");
    } catch(e) { alert("Replay failed"); }
  };

  const handleEval = async () => {
    if (!selectedTrace) return;
    if (!confirm("Trigger system evaluation for this trace?")) return;
    try {
      await axios.post(`/v1/trace_evaluate?trace_id=${selectedTrace}`);
      alert("Evaluation task triggered.");
    } catch(e) { alert("Eval failed"); }
  };

  let rootCauseError: any = null;
  if (traceData?.runs) {
    for (const r of traceData.runs) {
      if (r.status === 'error' && r.outputs) {
        rootCauseError = r.outputs.exception || r.outputs.error || r.outputs.detail || null;
        if (rootCauseError) break;
      }
    }
  }

  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const yesterday = new Date(today);
  yesterday.setDate(yesterday.getDate() - 1);

  const groupedTraces: { label: string, traces: TraceMeta[] }[] = [
    { label: 'Today', traces: [] },
    { label: 'Yesterday', traces: [] },
    { label: 'Older', traces: [] }
  ];

  tracesMeta.forEach(meta => {
    const d = new Date(meta.start_time * 1000);
    if (d >= today) groupedTraces[0].traces.push(meta);
    else if (d >= yesterday) groupedTraces[1].traces.push(meta);
    else groupedTraces[2].traces.push(meta);
  });

  return (
    <div className="app-container">
      {/* Sidebar */}
      <div className="sidebar glass-panel">
        <div className="sidebar-header">
          <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
            <Zap size={24} color="var(--accent-color)" className="glow-icon" />
            <h2 className="gradient-text">Navi Traces</h2>
          </div>
          <div style={{ display: 'flex', gap: 8 }}>
            <button
              className={`refresh-btn ${autoRefresh ? 'active success' : ''}`}
              onClick={() => setAutoRefresh(!autoRefresh)}
              title={autoRefresh ? "Auto-refresh is ON" : "Turn on auto-refresh"}
              style={{ color: autoRefresh ? 'var(--success-color)' : 'inherit' }}
            >
              {autoRefresh ? <Play size={16} /> : <Pause size={16} />}
            </button>
            <button
              className={`refresh-btn ${isRefreshing ? 'spinning' : ''}`}
              onClick={() => fetchTraceIds(true)}
              title="Refresh traces"
            >
              <RefreshCw size={16} />
            </button>
          </div>
        </div>
        <div style={{ padding: '0 15px 10px', display: 'flex', flexDirection: 'column', gap: 8 }}>
          <div className="search-box glass-panel" style={{ display: 'flex', alignItems: 'center', padding: '6px 12px', borderRadius: 6, border: '1px solid rgba(255, 255, 255, 0.1)', background: 'rgba(0,0,0,0.2)' }}>
             <Search size={14} color="var(--text-secondary)" style={{ marginRight: 8 }} />
             <input
               type="text"
               placeholder="Search all traces..."
               value={globalSearch}
               onChange={(e) => setGlobalSearch(e.target.value)}
               onKeyDown={(e) => { if (e.key === 'Enter') fetchTraceIds(true); }}
               style={{ background: 'transparent', border: 'none', color: '#fff', width: '100%', outline: 'none', fontSize: '0.85rem' }}
             />
          </div>
          <div style={{ display: 'flex', gap: 8 }}>
            <button
              className={`filter-btn ${listHasError ? 'active error' : ''}`}
              style={{ flex: 1, padding: '4px 8px', fontSize: '0.75rem' }}
              onClick={() => setListHasError(!listHasError)}
            >
              <XCircle size={12} style={{ marginRight: 4 }} />
              Failed Only
            </button>
            <button
              className="filter-btn"
              style={{ flex: 1, padding: '4px 8px', fontSize: '0.75rem' }}
              onClick={() => setListLimit(listLimit + 50)}
            >
              Load More
            </button>
          </div>
          <div style={{ display: 'flex', gap: 8 }}>
            <button
              className={`filter-btn ${listShowDaemon ? 'active' : ''}`}
              style={{ flex: 1, padding: '4px 8px', fontSize: '0.75rem' }}
              onClick={() => setListShowDaemon(!listShowDaemon)}
            >
              <Activity size={12} style={{ marginRight: 4 }} />
              {listShowDaemon ? 'Hide Daemon' : 'Show Daemon'}
            </button>
            <button
              className="filter-btn error"
              style={{ flex: 1, padding: '4px 8px', fontSize: '0.75rem' }}
              onClick={handleDeleteAll}
            >
              <Trash2 size={12} style={{ marginRight: 4 }} />
              Clear DB
            </button>
          </div>
        </div>
        <div className="trace-list">
          {tracesMeta.length === 0 && (
            <div style={{ padding: 20, textAlign: 'center', opacity: 0.5, fontSize: '0.85rem' }}>
              No traces recorded yet.
            </div>
          )}
          {groupedTraces.map((group) => group.traces.length > 0 && (
            <div key={group.label}>
              <div style={{ padding: '12px 16px 4px', fontSize: '0.75rem', fontWeight: 600, color: 'var(--text-secondary)', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
                {group.label}
              </div>
              {group.traces.map((meta) => (
                <div
                  key={meta.trace_id}
                  className={`trace-item ${selectedTrace === meta.trace_id ? 'active' : ''}`}
                  onClick={() => loadTrace(meta.trace_id)}
                >
                  <div className="trace-id" style={{ wordBreak: 'break-all', fontSize: '0.75rem', lineHeight: 1.4, color: meta.has_error ? 'var(--error-color)' : 'inherit', fontWeight: meta.has_error ? 600 : 'normal' }}>
                    {meta.has_error && <ShieldAlert size={12} style={{display: 'inline', marginRight: 4}}/>}
                    {meta.trace_id}
                  </div>
                  {meta.preview_text && (
                    <div style={{ marginTop: 6, fontSize: '0.8rem', opacity: 0.8, display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical', overflow: 'hidden', color: 'var(--text-secondary)' }}>
                      {meta.preview_text}
                    </div>
                  )}
                  <div className="trace-date" style={{ marginTop: 6, display: 'flex', justifyContent: 'space-between' }}>
                    <span>
                      <Clock size={12} style={{ display: 'inline', marginRight: 4, opacity: 0.7 }} />
                      {meta.trace_id.substring(0, 8)} {meta.trace_id.substring(9, 15).replace(/(..)(..)(..)/, '$1:$2:$3')}
                    </span>
                    {meta.duration > 0 && <span style={{ opacity: 0.8 }}><Timer size={12} style={{ display: 'inline', marginRight: 2 }} /> {meta.duration.toFixed(2)}s</span>}
                  </div>
                </div>
              ))}
            </div>
          ))}
        </div>
      </div>

      {/* Main Content */}
      <div className="main-content">
        {!selectedTrace && (
          <div className="empty-state">
            <div className="empty-icon-wrap">
              <Search size={48} />
            </div>
            <h2>Select a trace to view details</h2>
            <p className="empty-subtext">Waiting for real-time events...</p>
          </div>
        )}

        {loading && (
          <div className="empty-state">
            <div className="loader" />
            <p>Loading trace data...</p>
          </div>
        )}

        {traceData && (
          <>
            <div className="header glass-panel highlight-panel" style={{ padding: '24px', marginBottom: '32px' }}>
              <div className="header-info" style={{ width: '100%' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', width: '100%' }}>
                  <div>
                    <h1 style={{ marginBottom: 8 }}>Trace Overview</h1>
                    <div className="header-subtitle">
                      <span className="badge">ID</span> <span style={{ fontFamily: 'monospace', opacity: 0.8 }}>{selectedTrace}</span>
                    </div>
                  </div>

                  {/* Metrics Dashboard */}
                  <div style={{ display: 'flex', gap: 24, alignItems: 'center' }}>
                    <div className="metric-box">
                      <div className="metric-label"><Timer size={14} /> Duration</div>
                      <div className="metric-value">{(totalDuration).toFixed(2)}s</div>
                    </div>
                    <div className="metric-box">
                      <div className="metric-label"><Activity size={14} /> Total Tokens</div>
                      <div className="metric-value">{(totalTokensInput + totalTokensOutput).toLocaleString()}</div>
                    </div>
                    <div className="metric-box">
                      <div className="metric-label"><Database size={14} /> Est. Cost</div>
                      <div className="metric-value" style={{color: 'var(--success-color)'}}>${estimatedCost.toFixed(5)}</div>
                    </div>
                  </div>
                </div>

                <div style={{ marginTop: 24, display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap' }}>
                  <div className="mode-toggle glass-panel" style={{ display: 'flex', padding: 4, borderRadius: 8, background: 'rgba(0,0,0,0.3)' }}>
                    <button className={`toggle-btn ${viewMode === 'tree' ? 'active' : ''}`} onClick={() => setViewMode('tree')}>
                      <ListTree size={14} style={{marginRight: 6}}/> Tree View
                    </button>
                    <button className={`toggle-btn ${viewMode === 'chat' ? 'active' : ''}`} onClick={() => setViewMode('chat')}>
                      <MessageSquare size={14} style={{marginRight: 6}}/> Chat View
                    </button>
                  </div>
                  
                  {viewMode === 'tree' && (
                    <div style={{ display: 'flex', gap: 8, padding: '0 12px', borderLeft: '1px solid rgba(255,255,255,0.1)' }}>
                      <label style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: '0.85rem', cursor: 'pointer' }}>
                        <input type="checkbox" checked={filterLLM} onChange={e => setFilterLLM(e.target.checked)} /> LLM
                      </label>
                      <label style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: '0.85rem', cursor: 'pointer' }}>
                        <input type="checkbox" checked={filterTool} onChange={e => setFilterTool(e.target.checked)} /> Tool
                      </label>
                      <label style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: '0.85rem', cursor: 'pointer' }}>
                        <input type="checkbox" checked={filterEngine} onChange={e => setFilterEngine(e.target.checked)} /> Engine
                      </label>
                    </div>
                  )}

                  <div style={{ flexGrow: 1 }} />

                  <button className="filter-btn" style={{ background: 'rgba(59, 130, 246, 0.1)', color: '#60a5fa' }} onClick={handleReplay}>
                    <RotateCcw size={14} /> Replay
                  </button>
                  <button className="filter-btn" style={{ background: 'rgba(139, 92, 246, 0.1)', color: '#a78bfa' }} onClick={handleEval}>
                    <Rocket size={14} /> Auto-Eval
                  </button>
                  <button className="filter-btn highlight-btn" style={{ background: 'rgba(59, 130, 246, 0.1)', border: '1px solid rgba(59, 130, 246, 0.3)', color: '#60a5fa' }} onClick={downloadTrace}>
                    <Download size={14} /> JSON
                  </button>
                  <button className="filter-btn" style={{ color: 'var(--error-color)' }} onClick={handleDeleteTrace}>
                    <Trash2 size={14} /> Delete
                  </button>
                </div>

                {viewMode === 'tree' && (
                  <div style={{ marginTop: 20, display: 'flex', gap: 12, alignItems: 'center' }}>
                    <button
                      className={`filter-btn ${showErrorsOnly ? 'active error' : ''}`}
                      onClick={() => setShowErrorsOnly(!showErrorsOnly)}
                    >
                      <XCircle size={14} /> Show Errors Only
                    </button>
                    <div className="search-box glass-panel" style={{ display: 'flex', alignItems: 'center', padding: '6px 12px', borderRadius: 6, flexGrow: 1, marginLeft: 12, border: '1px solid rgba(255, 255, 255, 0.1)', background: 'rgba(0,0,0,0.2)' }}>
                      <Search size={14} color="var(--text-secondary)" style={{ marginRight: 8 }} />
                      <input
                        type="text"
                        placeholder="Search local tree runs..."
                        value={searchQuery}
                        onChange={(e) => setSearchQuery(e.target.value)}
                        style={{ background: 'transparent', border: 'none', color: '#fff', width: '100%', outline: 'none', fontSize: '0.85rem' }}
                      />
                    </div>
                  </div>
                )}
              </div>
            </div>

            {rootCauseError && (
              <div className="error-alert glass-panel" style={{ padding: '16px', background: 'rgba(239, 68, 68, 0.05)', border: '1px solid rgba(239, 68, 68, 0.3)', borderRadius: '8px', marginBottom: '24px' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, color: 'var(--error-color)', marginBottom: 8, fontWeight: 600 }}>
                  <ShieldAlert size={16} /> Root Cause Exception Extracted
                </div>
                <div style={{ fontFamily: 'monospace', fontSize: '0.8rem', whiteSpace: 'pre-wrap', color: '#fca5a5' }}>
                  {typeof rootCauseError === 'string' ? rootCauseError : JSON.stringify(rootCauseError, null, 2)}
                </div>
              </div>
            )}

            {viewMode === 'chat' ? (
              <div className="chat-view-container glass-panel" style={{ padding: 24, borderRadius: 8, display: 'flex', flexDirection: 'column', gap: 16 }}>
                {allRuns.filter(r => r.name === 'Channel Receive' || r.name === 'Channel Send').sort((a,b) => a.start_time - b.start_time).map(msg => {
                  const isUser = msg.name === 'Channel Receive';
                  const text = isUser ? (msg.inputs?.message?.text || msg.inputs?.message || msg.inputs?.text || JSON.stringify(msg.inputs)) : (msg.inputs?.message?.text || msg.inputs?.message || msg.inputs?.text || msg.outputs?.message?.text || msg.outputs?.message || JSON.stringify(msg.outputs));
                  return (
                    <div key={msg.id} style={{ alignSelf: isUser ? 'flex-end' : 'flex-start', maxWidth: '80%', background: isUser ? 'rgba(139, 92, 246, 0.2)' : 'rgba(0,0,0,0.3)', border: isUser ? '1px solid rgba(139, 92, 246, 0.3)' : '1px solid rgba(255,255,255,0.1)', padding: '12px 16px', borderRadius: 12, borderBottomRightRadius: isUser ? 0 : 12, borderBottomLeftRadius: !isUser ? 0 : 12 }}>
                      <div style={{ fontSize: '0.75rem', opacity: 0.5, marginBottom: 6, textTransform: 'uppercase', display: 'flex', alignItems: 'center', gap: 6 }}>
                        {isUser ? <Inbox size={12}/> : <Send size={12}/>}
                        {isUser ? 'User' : 'Assistant'}
                      </div>
                      <div className="markdown-body" style={{ fontSize: '0.95rem' }}>
                        <ReactMarkdown>{typeof text === 'string' ? text : JSON.stringify(text)}</ReactMarkdown>
                      </div>
                    </div>
                  );
                })}
              </div>
            ) : (
              <div className="tree-container">
                {rootRuns.length === 0 ? (
                  <div className="empty-state glass-panel" style={{ padding: 40, marginTop: 20 }}>
                    <Activity size={32} />
                    <p>No valid run hierarchy found in this trace.</p>
                  </div>
                ) : (
                  rootRuns.map(rootRun => (
                     <RunNode
                        key={rootRun.id}
                        run={rootRun}
                        allRuns={allRuns}
                        traceTotalDuration={totalDuration}
                        firstTime={firstTime}
                        depth={0}
                        autoExpand={errorPaths.has(rootRun.id)}
                        showLLM={filterLLM}
                        showTool={filterTool}
                        showEngine={filterEngine}
                     />
                  ))
                )}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

export default App;
