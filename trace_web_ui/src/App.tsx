import { useEffect, useState } from 'react';
import axios from 'axios';
import { format } from 'date-fns';
import { Activity, Code, CheckCircle2, XCircle, Search, Clock, ChevronDown, ChevronRight, Zap, Copy, Check, RefreshCw, Timer, Hash, ShieldAlert } from 'lucide-react';
import { JsonView, darkStyles } from 'react-json-view-lite';
import 'react-json-view-lite/dist/index.css';
import type { TraceData, TraceEvent } from './types';
import './App.css';

type EventUiState = {
  kind: 'success' | 'approval' | 'blocked' | 'error';
  label: string;
  message: string | null;
  bannerTitle: string;
};

const parseJsonObject = (value: string): Record<string, any> => {
  try {
    const parsed = JSON.parse(value || '{}');
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {};
  } catch (e) {
    return {};
  }
};

const textValue = (value: unknown): string => {
  return typeof value === 'string' ? value : '';
};

const eventUiState = (event: TraceEvent): EventUiState => {
  if (event.ok) {
    return {
      kind: 'success',
      label: 'SUCCESS',
      message: null,
      bannerTitle: '',
    };
  }

  const parsedOut = parseJsonObject(event.output_json);
  const parsedIn = parseJsonObject(event.input_json);
  const facts = parsedOut.facts && typeof parsedOut.facts === 'object' ? parsedOut.facts : {};
  const reason = textValue(facts.reason || parsedOut.reason || parsedOut.error || parsedOut.message || parsedIn.error || parsedIn.reason);
  const approvalRequired = (
    event.phase === 'capability.result' &&
    (
      facts.entity_type === 'approval_request' ||
      reason === 'sensitive_op_requires_approval' ||
      reason === 'workflow_approval_required' ||
      parsedOut.error_reason === 'approval_required'
    )
  );
  if (approvalRequired) {
    return {
      kind: 'approval',
      label: 'APPROVAL REQUIRED',
      message: reason || 'approval_required',
      bannerTitle: 'Action Paused:',
    };
  }

  const lowerReason = reason.toLowerCase();
  const blocked = lowerReason.includes('sandbox') || lowerReason.includes('block') || lowerReason.includes('deny') || lowerReason.includes('denied') || lowerReason.includes('unauthorized') || lowerReason.includes('approval');
  if (blocked) {
    return {
      kind: 'blocked',
      label: 'BLOCKED',
      message: reason,
      bannerTitle: 'Action Intercepted:',
    };
  }

  return {
    kind: 'error',
    label: 'FAILED',
    message: reason,
    bannerTitle: 'Error Detected:',
  };
};

const CollapsibleJson = ({ title, jsonStr }: { title: string, jsonStr: string }) => {
  const [isOpen, setIsOpen] = useState(false);
  const [copied, setCopied] = useState(false);

  if (!jsonStr || jsonStr === '{}') return null;

  const renderJson = () => {
    try {
      const parsed = typeof jsonStr === 'string' ? JSON.parse(jsonStr) : jsonStr;
      return <JsonView data={parsed} shouldExpandNode={(level) => level < 2} style={darkStyles} />;
    } catch (e) {
      return <pre className="code-block" style={{ margin: 0, fontSize: '0.85rem' }}>{jsonStr}</pre>;
    }
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
        <div className="code-container" style={{ overflowX: 'auto', background: '#1e1e1e', padding: '12px 16px' }}>
          {renderJson()}
        </div>
      )}
    </div>
  );
};

function App() {
  const [traceIds, setTraceIds] = useState<string[]>([]);
  const [selectedTrace, setSelectedTrace] = useState<string | null>(null);
  const [traceData, setTraceData] = useState<TraceData | null>(null);
  const [loading, setLoading] = useState(false);
  const [isRefreshing, setIsRefreshing] = useState(false);

  // Filters
  const [showErrorsOnly, setShowErrorsOnly] = useState(false);
  const [showToolsOnly, setShowToolsOnly] = useState(false);

  useEffect(() => {
    fetchTraceIds();
    // Auto polling every 3 seconds
    const interval = setInterval(() => {
      fetchTraceIds(false);
    }, 3000);
    return () => clearInterval(interval);
  }, []);

  const fetchTraceIds = async (showRefresh = false) => {
    if (showRefresh) setIsRefreshing(true);
    try {
      const res = await axios.get('/v1/traces');
      // Robustly handle both enveloped and raw responses
      const rawData = res.data;
      const actualData = (rawData && rawData.data && rawData.data.trace_ids) ? rawData.data : rawData;
      const ids = actualData.trace_ids || [];
      const sortedIds = [...ids].sort().reverse();
      setTraceIds(sortedIds);
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
        <div className="trace-list">
          {traceIds.length === 0 && (
            <div style={{ padding: 20, textAlign: 'center', opacity: 0.5, fontSize: '0.85rem' }}>
              No traces recorded yet.
            </div>
          )}
          {traceIds.map((id) => (
            <div
              key={id}
              className={`trace-item ${selectedTrace === id ? 'active' : ''}`}
              onClick={() => loadTrace(id)}
            >
              <div className="trace-id">{id.substring(16)}</div>
              <div className="trace-date">
                <Clock size={12} style={{ display: 'inline', marginRight: 4, opacity: 0.7 }} />
                {id.substring(0, 8)} {id.substring(9, 15).replace(/(..)(..)(..)/, '$1:$2:$3')}
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
                <div style={{ marginTop: 20, display: 'flex', gap: 12 }}>
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
                </div>
              </div>
            </div>

            <div className="timeline">
              {traceData.events.map((event: TraceEvent, idx: number) => {
                // Apply filters
                if (showErrorsOnly && event.ok) return null;
                if (showToolsOnly && !event.tool) return null;

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

                const uiState = eventUiState(event);
                const isAttention = uiState.kind === 'approval' || uiState.kind === 'blocked';

                return (
                <div key={event.id || idx} className="timeline-event fade-in" style={{ animationDelay: `${idx * 0.05}s` }}>
                  <div className="timeline-line"></div>
                  <div className={`timeline-icon ${uiState.kind} glow-icon-sm`}>
                    {uiState.kind === 'success' ? <CheckCircle2 size={18} /> : isAttention ? <ShieldAlert size={18} /> : <XCircle size={18} />}
                  </div>
                  <div className="timeline-content glass-panel" style={{ borderColor: !event.ok ? (isAttention ? 'rgba(245, 158, 11, 0.4)' : 'rgba(239, 68, 68, 0.4)') : undefined }}>
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
                      <div className={`event-status status-${uiState.kind}`}>
                        {uiState.label}
                      </div>
                    </div>
                    
                    <div className="event-body">
                      {/* Critical Error Banner if extracted */}
                      {uiState.message && (
                        <div className={isAttention ? "error-banner blocked-banner" : "error-banner"}>
                          {isAttention ? <ShieldAlert size={16} /> : <XCircle size={16} />}
                          <strong>{uiState.bannerTitle}</strong> {uiState.message}
                        </div>
                      )}

                      <CollapsibleJson title="Input Payload" jsonStr={event.input_json} />
                      <CollapsibleJson title="Output Response" jsonStr={event.output_json} />

                      {event.message && (
                        <div className="message-box">
                          {event.message}
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
               traceData.events.filter(e => (!showErrorsOnly || !e.ok) && (!showToolsOnly || e.tool)).length === 0 && (
                <div className="empty-state glass-panel" style={{ padding: 40, marginTop: 20 }}>
                  <Search size={32} />
                  <p>All events filtered out by your current filters.</p>
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
