import { useEffect, useState } from 'react';
import axios from 'axios';
import { format } from 'date-fns';
import { Activity, Code, CheckCircle2, XCircle, Search, Clock, ChevronDown, ChevronRight, Zap, Copy, Check, RefreshCw, Timer, Hash, ShieldAlert, UserCheck } from 'lucide-react';
import { JsonView } from 'react-json-view-lite';
import 'react-json-view-lite/dist/index.css';
import ReactMarkdown from 'react-markdown';
import type { TraceData, TraceEvent, TraceMeta } from './types';
import './App.css';


const CollapsibleJson = ({ title, jsonStr }: { title: string, jsonStr: string }) => {
  const [isOpen, setIsOpen] = useState(false);
  const [copied, setCopied] = useState(false);

  if (!jsonStr || jsonStr === '{}') return null;

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
      // If it's an unquoted string or invalid JSON, treat the raw string as the value
      parsed = jsonStr;
    }
    
    // JsonView expects an object or array. If it's a primitive, wrap it nicely.
    if (typeof parsed !== 'object' || parsed === null) {
      parsed = { payload: parsed };
    }

    return <JsonView data={parsed} shouldExpandNode={(level) => level < 2} style={customJsonStyle} />;
  };

  const handleCopy = (e: React.MouseEvent) => {
    e.stopPropagation();
    let textToCopy = jsonStr;
    try {
      if (typeof jsonStr === 'string') {
        textToCopy = JSON.stringify(JSON.parse(jsonStr), null, 2);
      }
    } catch (e) {}
    navigator.clipboard.writeText(textToCopy);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="data-section">
      <div className="data-title" onClick={() => setIsOpen(!isOpen)}>
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
        <div className="code-container glass-panel">
          {renderJson()}
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
  const [showToolsOnly, setShowToolsOnly] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');

  useEffect(() => {
    fetchTraceIds();
    // Auto polling every 3 seconds
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
      // Robustly handle both enveloped and raw responses
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

  // Calculate metrics
  let totalDuration = 0;
  let eventCount = 0;

  if (traceData && traceData.events && traceData.events.length > 0) {
    eventCount = traceData.events.length;
    const firstEventTime = traceData.events[0].created_at;
    const lastEventTime = traceData.events[traceData.events.length - 1].created_at;
    totalDuration = lastEventTime - firstEventTime;
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
                      <div className="metric-label"><Hash size={14} /> Events</div>
                      <div className="metric-value">{eventCount}</div>
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
                  <button 
                    className={`filter-btn ${showToolsOnly ? 'active info' : ''}`}
                    onClick={() => setShowToolsOnly(!showToolsOnly)}
                  >
                    <Code size={14} /> Show Tool Calls Only
                  </button>
                  
                  {/* Search Bar */}
                  <div className="search-box glass-panel" style={{ display: 'flex', alignItems: 'center', padding: '6px 12px', borderRadius: 6, flexGrow: 1, marginLeft: 12, border: '1px solid rgba(255, 255, 255, 0.1)', background: 'rgba(0,0,0,0.2)' }}>
                    <Search size={14} color="var(--text-secondary)" style={{ marginRight: 8 }} />
                    <input 
                      type="text" 
                      placeholder="Deep Search in payloads and messages..." 
                      value={searchQuery}
                      onChange={(e) => setSearchQuery(e.target.value)}
                      style={{ background: 'transparent', border: 'none', color: '#fff', width: '100%', outline: 'none', fontSize: '0.85rem' }}
                    />
                  </div>
                </div>
              </div>
            </div>

            <div className="timeline">
              {traceData.events.map((event: TraceEvent, idx: number) => {
                 let extractedError = null;
                 let isBlocked = false;
                 let isApproval = false;

                if (!event.ok) {
                  const phase = (event.phase || '').toLowerCase();
                  if (phase === 'approval') isApproval = true;
                  else if (phase === 'sandbox' || phase === 'verifier') isBlocked = true;

                  try {
                    const parsedOut = JSON.parse(event.output_json || '{}');
                    const parsedIn = JSON.parse(event.input_json || '{}');
                    extractedError = parsedOut.error || parsedOut.message || parsedIn.error || parsedIn.reason || null;
                    
                    if (extractedError && typeof extractedError === 'string') {
                      const lowerError = extractedError.toLowerCase();
                      if (lowerError.includes('approval') || lowerError.includes('human')) {
                        isApproval = true;
                        isBlocked = false; // Override block if it specifically asks for approval
                      } else if (lowerError.includes('sandbox') || lowerError.includes('block') || lowerError.includes('deny') || lowerError.includes('denied') || lowerError.includes('unauthorized')) {
                        if (!isApproval) isBlocked = true;
                      }
                    }
                  } catch (e) {}
                }
                // Deep Search Match logic
                let matchesSearch = true;
                if (searchQuery.trim() !== '') {
                  const q = searchQuery.toLowerCase();
                  matchesSearch = false;
                  if (event.tool?.toLowerCase().includes(q) || 
                      event.phase?.toLowerCase().includes(q) || 
                      event.message?.toLowerCase().includes(q) || 
                      event.input_json?.toLowerCase().includes(q) || 
                      event.output_json?.toLowerCase().includes(q)) {
                    matchesSearch = true;
                  }
                }

                // Apply filters
                if (!matchesSearch) return null;
                if (showErrorsOnly && event.ok) return null;
                if (showToolsOnly && !event.tool) return null;

                const firstTime = traceData.events[0]?.created_at || 0;
                const lastTime = traceData.events[traceData.events.length - 1]?.created_at || firstTime;
                const traceTotalDuration = Math.max(0.001, lastTime - firstTime);
                const eventStart = Math.max(0, event.created_at - firstTime);
                const nextEventTime = idx < traceData.events.length - 1
                  ? traceData.events[idx + 1].created_at
                  : event.created_at;
                const eventDuration = Math.max(0, nextEventTime - event.created_at);
                const leftPercent = (eventStart / traceTotalDuration) * 100;
                const widthPercent = Math.max(0.5, (eventDuration / traceTotalDuration) * 100);

                // Calculate duration of THIS event (time since previous event)
                let eventDurationStr = '';
                let isSlow = false;
                if (idx > 0) {
                  const prevEvent = traceData.events[idx - 1];
                  const duration = event.created_at - prevEvent.created_at;
                  if (duration >= 0.01) {
                    eventDurationStr = `(+${duration.toFixed(2)}s)`;
                    if (duration > 3.0) isSlow = true; // Highlight if takes more than 3s
                  }
                }

                return (
                <div key={event.id || idx} className="timeline-event fade-in" style={{ animationDelay: `${idx * 0.05}s` }}>
                  <div className="timeline-line"></div>
                  <div className={`timeline-icon ${event.ok ? 'success' : isApproval ? 'approval' : isBlocked ? 'blocked' : 'error'} glow-icon-sm`}>
                    {event.ok ? <CheckCircle2 size={18} /> : isApproval ? <UserCheck size={18} /> : isBlocked ? <ShieldAlert size={18} /> : <XCircle size={18} />}
                  </div>
                  <div className="timeline-content glass-panel" style={{ borderColor: !event.ok ? (isApproval ? 'rgba(168, 85, 247, 0.4)' : isBlocked ? 'rgba(245, 158, 11, 0.4)' : 'rgba(239, 68, 68, 0.4)') : undefined }}>
                    <div className="event-header">
                      <div>
                        <h3 className="event-title">
                          <span className="phase-badge">{event.phase}</span>
                          {event.tool && <span className="tool-name">{event.tool}</span>}
                        </h3>
                        <div className="event-subtitle">
                          <span className="time">{format(new Date(event.created_at * 1000), 'HH:mm:ss.SSS')}</span>
                          {eventDurationStr && (
                            <span style={{ color: isSlow ? '#fbbf24' : 'var(--text-secondary)', fontWeight: isSlow ? 700 : 400, marginLeft: 6 }}>
                              {isSlow && '⏳'} {eventDurationStr}
                            </span>
                          )}
                          <span className="separator">•</span>
                          Source: <span className="highlight-text">{event.source}</span>
                          <span className="separator">•</span>
                          Role: <span className="highlight-text">{event.model_role || 'system'}</span>
                        </div>
                      </div>
                      <div className={`event-status ${event.ok ? 'status-success' : isApproval ? 'status-approval' : isBlocked ? 'status-blocked' : 'status-error'}`}>
                        {event.ok ? 'SUCCESS' : isApproval ? 'NEEDS APPROVAL' : isBlocked ? 'BLOCKED' : 'FAILED'}
                      </div>
                    </div>
                    
                    {/* Waterfall Bar */}
                    <div className="waterfall-container" style={{ width: '100%', height: '4px', background: 'rgba(255,255,255,0.05)', borderRadius: '2px', marginBottom: '16px', position: 'relative' }} title={`Duration: ${eventDuration.toFixed(3)}s`}>
                      <div style={{
                        position: 'absolute',
                        left: `${leftPercent}%`,
                        width: `${widthPercent}%`,
                        height: '100%',
                        background: event.ok ? 'var(--accent-color)' : 'var(--error-color)',
                        borderRadius: '2px',
                        boxShadow: `0 0 8px ${event.ok ? 'var(--accent-color)' : 'var(--error-color)'}`
                      }} />
                    </div>

                    <div className="event-body">
                      {/* Critical Error Banner if extracted */}
                      {extractedError && (
                        <div className={isApproval ? "error-banner approval-banner" : isBlocked ? "error-banner blocked-banner" : "error-banner"}>
                          {isApproval ? <UserCheck size={16} /> : isBlocked ? <ShieldAlert size={16} /> : <XCircle size={16} />}
                          <strong>{isApproval ? 'Approval Required:' : isBlocked ? 'Action Intercepted:' : 'Error Detected:'}</strong> {String(extractedError)}
                        </div>
                      )}

                      <CollapsibleJson title="Input Payload" jsonStr={event.input_json} />
                      <CollapsibleJson title="Output Response" jsonStr={event.output_json} />

                      {event.message && (
                        <div className="message-box markdown-body" style={{ background: 'rgba(0,0,0,0.3)', padding: '16px', borderRadius: '8px', marginTop: '12px', borderLeft: '3px solid var(--accent-color)' }}>
                          <ReactMarkdown>
                            {event.message}
                          </ReactMarkdown>
                        </div>
                      )}
                    </div>
                  </div>
                </div>
              );
              })}
              
              {(!traceData.events || traceData.events.length === 0) && (
                <div className="empty-state glass-panel" style={{ padding: 40, marginTop: 20 }}>
                  <Activity size={32} />
                  <p>No events found in this trace.</p>
                </div>
              )}
              
              {/* If filtered out everything */}
              {traceData.events && traceData.events.length > 0 && 
               traceData.events.filter(e => {
                  let matchSearch = true;
                  if (searchQuery.trim() !== '') {
                    const q = searchQuery.toLowerCase();
                    if (!e.tool?.toLowerCase().includes(q) && 
                        !e.phase?.toLowerCase().includes(q) && 
                        !e.message?.toLowerCase().includes(q) && 
                        !e.input_json?.toLowerCase().includes(q) && 
                        !e.output_json?.toLowerCase().includes(q)) {
                      matchSearch = false;
                    }
                  }
                  return matchSearch && (!showErrorsOnly || !e.ok) && (!showToolsOnly || e.tool);
               }).length === 0 && (
                <div className="empty-state glass-panel" style={{ padding: 40, marginTop: 20 }}>
                  <Search size={32} />
                  <p>All events filtered out by your current filters or search query.</p>
                </div>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

export default App;
