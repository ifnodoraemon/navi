import { useEffect, useState } from 'react';
import axios from 'axios';
import { format } from 'date-fns';
import { Activity, Code, CheckCircle2, XCircle, Search, Clock, ChevronDown, ChevronRight, Zap, Copy, Check, RefreshCw, Timer, ShieldAlert, Layers, Inbox, Send } from 'lucide-react';
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

  return (
    <div className="data-section">
      <div className="data-title" onClick={(e) => { e.stopPropagation(); setIsOpen(!isOpen); }}>
        <div style={{ display: 'flex', alignItems: 'center' }}>
          {isOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
          <Code size={14} style={{ marginRight: 6, marginLeft: 4 }} />
          <span>{title}</span>
        </div>
        {isOpen && (
          <button className="copy-btn" onClick={handleCopy} title="Copy to clipboard">
            {copied ? <Check size={14} color="var(--success-color)" /> : <Copy size={14} />}
          </button>
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
  autoExpand = false
}: {
  run: TraceRunView,
  allRuns: TraceRunView[],
  traceTotalDuration: number,
  firstTime: number,
  depth?: number,
  autoExpand?: boolean
}) => {
  const [expanded, setExpanded] = useState(depth < 2 || autoExpand);

  const children = allRuns.filter(r => r.parent_run_id === run.id).sort((a,b) => a.start_time - b.start_time);
  const hasChildren = children.length > 0;

  const eventStart = Math.max(0, run.start_time - firstTime);
  const eventDuration = Math.max(0, run.end_time - run.start_time);
  const leftPercent = traceTotalDuration > 0 ? (eventStart / traceTotalDuration) * 100 : 0;
  const widthPercent = traceTotalDuration > 0 ? Math.max(0.5, (eventDuration / traceTotalDuration) * 100) : 100;

  const isError = run.status === 'error';
  const isBlocked = run.status === 'blocked';

  const statusClass = isError ? 'error' : isBlocked ? 'blocked' : 'success';
  let StatusIcon = isError ? XCircle : isBlocked ? ShieldAlert : CheckCircle2;
  if (run.name === 'Channel Receive') StatusIcon = Inbox;
  if (run.name === 'Channel Send') StatusIcon = Send;

  // Custom renders based on run_type
  let runContent = null;
  if (!hasChildren && expanded) {
    if (run.run_type === 'llm') {
      const inputs = run.inputs || {};
      const outputs = run.outputs || {};
      const prompt = inputs.message || inputs.prompt || inputs.system_prompt;

      runContent = (
        <div className="run-details">
          {prompt && (
            <div className="message-box markdown-body" style={{ background: 'rgba(255,255,255,0.03)', padding: '12px', borderRadius: '6px', marginBottom: '8px', borderLeft: '3px solid var(--text-secondary)' }}>
              <div style={{ fontSize: '0.75rem', opacity: 0.6, marginBottom: 4, textTransform: 'uppercase' }}>Prompt</div>
              <ReactMarkdown>{typeof prompt === 'string' ? prompt : JSON.stringify(prompt)}</ReactMarkdown>
            </div>
          )}
          {outputs.text || outputs.message ? (
             <div className="message-box markdown-body" style={{ background: 'rgba(0,0,0,0.3)', padding: '12px', borderRadius: '6px', borderLeft: '3px solid var(--accent-color)' }}>
               <div style={{ fontSize: '0.75rem', opacity: 0.6, marginBottom: 4, textTransform: 'uppercase' }}>Completion</div>
               <ReactMarkdown>{outputs.text || outputs.message}</ReactMarkdown>
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
      // Default generic JSON view
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
          {hasChildren && (
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
                />
              ))}
            </div>
          )}
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

  useEffect(() => {
    fetchTraceIds();
    const interval = setInterval(() => {
      fetchTraceIds(false);
    }, 3000);
    return () => clearInterval(interval);
  }, [listHasError, listShowDaemon, listLimit]);

  const fetchTraceIds = async (showRefresh = false) => {
    if (showRefresh) setIsRefreshing(true);
    try {
      const params: any = { limit: listLimit };
      if (listHasError) {
        params.has_error = true;
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
    });
  }

  return (
    <div className="app-container">
      {/* Sidebar */}
      <div className="sidebar glass-panel">
        <div className="sidebar-header">
          <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
            <Zap size={24} color="var(--accent-color)" className="glow-icon" />
            <h2 className="gradient-text">Navi Traces</h2>
          </div>
          <button
            className={`refresh-btn ${isRefreshing ? 'spinning' : ''}`}
            onClick={() => fetchTraceIds(true)}
            title="Refresh traces"
          >
            <RefreshCw size={16} />
          </button>
        </div>
        <div style={{ padding: '0 15px 10px', display: 'flex', flexDirection: 'column', gap: 8 }}>
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
          </div>
        </div>
        <div className="trace-list">
          {tracesMeta.length === 0 && (
            <div style={{ padding: 20, textAlign: 'center', opacity: 0.5, fontSize: '0.85rem' }}>
              No traces recorded yet.
            </div>
          )}
          {tracesMeta.map((meta) => (
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
                      <div className="metric-label"><Timer size={14} /> Total Duration</div>
                      <div className="metric-value">{(totalDuration).toFixed(2)}s</div>
                    </div>
                    <div className="metric-box">
                      <div className="metric-label"><Layers size={14} /> Total Runs</div>
                      <div className="metric-value">{allRuns.length}</div>
                    </div>
                  </div>
                </div>

                {/* Filters Row */}
                <div style={{ marginTop: 20, display: 'flex', gap: 12, alignItems: 'center' }}>
                  <button
                    className={`filter-btn ${showErrorsOnly ? 'active error' : ''}`}
                    onClick={() => setShowErrorsOnly(!showErrorsOnly)}
                  >
                    <XCircle size={14} /> Show Errors Only
                  </button>

                  {/* Search Bar */}
                  <div className="search-box glass-panel" style={{ display: 'flex', alignItems: 'center', padding: '6px 12px', borderRadius: 6, flexGrow: 1, marginLeft: 12, border: '1px solid rgba(255, 255, 255, 0.1)', background: 'rgba(0,0,0,0.2)' }}>
                    <Search size={14} color="var(--text-secondary)" style={{ marginRight: 8 }} />
                    <input
                      type="text"
                      placeholder="Search runs..."
                      value={searchQuery}
                      onChange={(e) => setSearchQuery(e.target.value)}
                      style={{ background: 'transparent', border: 'none', color: '#fff', width: '100%', outline: 'none', fontSize: '0.85rem' }}
                    />
                  </div>
                </div>
              </div>
            </div>

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
                   />
                ))
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

export default App;
