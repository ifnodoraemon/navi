import { useCallback, useEffect, useState } from 'react';
import axios from 'axios';
import { format } from 'date-fns';
import { Activity, Code, CheckCircle2, XCircle, Search, Clock, ChevronDown, ChevronRight, Zap, Copy, Check, RefreshCw, Timer, ShieldAlert, Layers, Inbox, Send, Download, Play, Pause, Trash2, RotateCcw, Rocket, ListTree, MessageSquare, Database, BarChart2 } from 'lucide-react';
import { JsonView } from 'react-json-view-lite';
import 'react-json-view-lite/dist/index.css';
import ReactMarkdown from 'react-markdown';
import { PrismLight as SyntaxHighlighter } from 'react-syntax-highlighter';
import bash from 'react-syntax-highlighter/dist/esm/languages/prism/bash';
import json from 'react-syntax-highlighter/dist/esm/languages/prism/json';
import markdown from 'react-syntax-highlighter/dist/esm/languages/prism/markdown';
import python from 'react-syntax-highlighter/dist/esm/languages/prism/python';
import typescript from 'react-syntax-highlighter/dist/esm/languages/prism/typescript';
import yaml from 'react-syntax-highlighter/dist/esm/languages/prism/yaml';
import { vscDarkPlus } from 'react-syntax-highlighter/dist/esm/styles/prism';
import type { TraceData, TraceMeta, TraceRunView } from './types';
import './App.css';

SyntaxHighlighter.registerLanguage('bash', bash);
SyntaxHighlighter.registerLanguage('json', json);
SyntaxHighlighter.registerLanguage('markdown', markdown);
SyntaxHighlighter.registerLanguage('python', python);
SyntaxHighlighter.registerLanguage('typescript', typescript);
SyntaxHighlighter.registerLanguage('yaml', yaml);

const SmartMarkdown = ({ children }: { children: string }) => {
  return (
    <ReactMarkdown
      components={{
        code({node: _node, className, children, ...props}: any) {
          const match = /language-(\w+)/.exec(className || '')
          return match ? (
            <SyntaxHighlighter
              style={vscDarkPlus as any}
              language={match[1]}
              PreTag="div"
              {...props}
            >
              {String(children).replace(/\n$/, '')}
            </SyntaxHighlighter>
          ) : (
            <code className={className} {...props}>
              {children}
            </code>
          )
        }
      }}
    >
      {children}
    </ReactMarkdown>
  );
};

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
    } catch {
      parsed = jsonStr;
    }

    if (typeof parsed === 'string') {
      if (parsed.includes('\n') || parsed.includes('```') || parsed.includes('**')) {
        return (
          <div className="markdown-body" style={{ fontSize: '0.85rem' }}>
            <SmartMarkdown>{parsed}</SmartMarkdown>
          </div>
        );
      } else {
        return (
          <div style={{ whiteSpace: 'pre-wrap', fontFamily: 'monospace', fontSize: '0.8rem', color: '#e5e7eb' }}>
            {parsed}
          </div>
        );
      }
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
    } catch {}
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
  showEngine = true,
  bottleneckRunId = ''
}: {
  run: TraceRunView,
  allRuns: TraceRunView[],
  traceTotalDuration: number,
  firstTime: number,
  depth?: number,
  autoExpand?: boolean,
  showLLM?: boolean,
  showTool?: boolean,
  showEngine?: boolean,
  bottleneckRunId?: string
}) => {
  const [expanded, setExpanded] = useState(depth < 2 || autoExpand);

  const children = allRuns.filter(r => r.parent_run_id === run.id).sort((a,b) => a.start_time - b.start_time);
  const hasChildren = children.length > 0;

  const eventStart = Math.max(0, run.start_time - firstTime);
  const eventDuration = Math.max(0, run.end_time - run.start_time);
  const leftPercent = traceTotalDuration > 0 ? (eventStart / traceTotalDuration) * 100 : 0;
  const widthPercent = traceTotalDuration > 0 ? Math.max(0.5, (eventDuration / traceTotalDuration) * 100) : 100;

  const isVisible = (run.run_type === 'llm' && showLLM) || (run.run_type === 'tool' && showTool) || (run.run_type === 'engine' && showEngine) || (!['llm', 'tool', 'engine'].includes(run.run_type));

  const childrenContent = hasChildren ? (
    <div className="run-children">
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
          bottleneckRunId={bottleneckRunId}
        />
      ))}
    </div>
  ) : null;

  if (!isVisible) {
    return <>{childrenContent}</>;
  }

  const isError = run.status === 'error';
  const isBlocked = run.status === 'blocked';
  const isRunning = run.status === 'running';

  const statusClass = isError ? 'error' : isBlocked ? 'blocked' : isRunning ? 'running' : 'success';
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
              <SmartMarkdown>{typeof prompt === 'string' ? prompt : JSON.stringify(prompt)}</SmartMarkdown>
            </div>
          )}
          {completionStr ? (
             <div className="message-box markdown-body" style={{ background: 'rgba(0,0,0,0.3)', padding: '12px', borderRadius: '6px', borderLeft: '3px solid var(--accent-color)' }}>
               <div style={{ fontSize: '0.75rem', opacity: 0.6, marginBottom: 4, textTransform: 'uppercase' }}>Completion</div>
               <SmartMarkdown>{completionStr}</SmartMarkdown>
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
      const nodeCost = ((usage.prompt_tokens || 0) * 0.005 / 1000) + ((usage.completion_tokens || 0) * 0.015 / 1000);
      tokenDisplay = (
        <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
          <span style={{ marginLeft: 8, padding: '2px 6px', background: 'rgba(59, 130, 246, 0.1)', color: '#60a5fa', borderRadius: 4, fontSize: '0.65rem', border: '1px solid rgba(59,130,246,0.2)' }} title={`Prompt: ${usage.prompt_tokens || 0} | Completion: ${usage.completion_tokens || 0}`}>
            {usage.total_tokens} tokens
          </span>
          <span style={{ padding: '2px 6px', background: 'rgba(16, 185, 129, 0.1)', color: '#34d399', borderRadius: 4, fontSize: '0.65rem', border: '1px solid rgba(16, 185, 129, 0.2)' }}>
            ${nodeCost.toFixed(5)}
          </span>
        </div>
      );
    }
  }

  let initiator = '';
  if (run.run_type === 'tool') {
    const parent = allRuns.find(r => r.id === run.parent_run_id);
    initiator = (parent && parent.run_type === 'llm') ? 'MODEL CALL' : 'ENGINE CALL';
  } else if (run.run_type === 'llm') {
    initiator = 'MODEL';
  } else if (run.run_type === 'engine') {
    initiator = 'ENGINE';
  }

  const isBottleneck = run.id === bottleneckRunId;

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
              {isRunning ? <RefreshCw size={16} className="spin-icon" style={{ color: '#f59e0b' }} /> : <StatusIcon size={16} />}
           </div>
           <div className="run-title-area">
              <div className="run-name">
                 {run.run_type !== 'chain' && (
                   <span className={`run-type-badge type-${run.run_type}`}>{run.run_type}</span>
                 )}
                 {initiator && (
                   <span style={{ marginRight: 8, fontSize: '0.65rem', fontWeight: 600, padding: '2px 6px', borderRadius: 4, background: 'rgba(255,255,255,0.1)', color: 'var(--text-secondary)', letterSpacing: '0.02em' }}>
                     {initiator}
                   </span>
                 )}
                 {isBottleneck && (
                   <span style={{ marginRight: 8, fontSize: '0.65rem', fontWeight: 600, padding: '2px 6px', borderRadius: 4, background: 'rgba(239, 68, 68, 0.15)', color: '#ef4444', letterSpacing: '0.02em', border: '1px solid rgba(239, 68, 68, 0.4)' }} title="This step consumed the most exclusive execution time">
                     🔥 BOTTLENECK
                   </span>
                 )}
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


const stringToColor = (str: string) => {
  if (!str) return 'transparent';
  let hash = 0;
  for (let i = 0; i < str.length; i++) {
    hash = str.charCodeAt(i) + ((hash << 5) - hash);
  }
  const hue = Math.abs(hash) % 360;
  return `hsl(${hue}, 70%, 60%)`;
};

const parseMaybeJson = (value: any): any => {
  if (!value) return null;
  if (typeof value !== 'string') return value;
  try {
    return JSON.parse(value);
  } catch {
    return value;
  }
};

const loopDecisionPayload = (decision: any): any => {
  const direct = parseMaybeJson(decision?.decision);
  if (direct && typeof direct === 'object') return direct;
  const output = parseMaybeJson(decision?.output_json);
  return output && typeof output === 'object' ? output : {};
};

const decisionTone = (decision: string) => {
  if (decision === 'blocked' || decision === 'failed') return '#fca5a5';
  if (decision === 'converged' || decision === 'finalize') return '#34d399';
  if (decision === 'recover') return '#fbbf24';
  return '#93c5fd';
};

const budgetValue = (value: any) => {
  if (value === null || value === undefined) return '-';
  if (typeof value === 'number') return Number.isInteger(value) ? value.toLocaleString() : value.toFixed(4);
  return String(value);
};

const sideEffectFromDecision = (payload: any): any => {
  const direct = payload?.evidence?.side_effect;
  if (direct && typeof direct === 'object') return direct;
  const transition = payload?.checker_results?.[0]?.evidence?.transition_evidence;
  const executor = transition?.executor;
  const facts = executor?.facts;
  if (!facts || typeof facts !== 'object') return null;
  const hasExplicitSideEffect = Boolean(
    facts.side_effect_scope ||
    facts.side_effect_state ||
    facts.side_effect_artifact ||
    facts.side_effect_commit ||
    facts.side_effect_compensate ||
    executor?.action === 'connector_outbound'
  );
  if (!hasExplicitSideEffect) return null;
  const scope = String(facts.side_effect_scope || '');
  const state = String(facts.side_effect_state || facts.state_transition || '');
  const artifact = String(facts.side_effect_artifact || facts.outbound_path || '');
  if (!scope && !state && !artifact) return null;
  return {
    scope,
    state,
    artifact,
    action: String(executor.action || ''),
    commit: String(facts.side_effect_commit || ''),
    compensate: String(facts.side_effect_compensate || ''),
  };
};

function App() {
  const [tracesMeta, setTracesMeta] = useState<TraceMeta[]>([]);
  const [selectedTrace, setSelectedTrace] = useState<string | null>(null);
  const [traceData, setTraceData] = useState<TraceData | null>(null);
  const [traceOffset, setTraceOffset] = useState(0);
  const [hasMoreEvents, setHasMoreEvents] = useState(false);
  const EVENTS_PER_PAGE = 200;
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
  const [viewMode, setViewMode] = useState<'tree' | 'chat' | 'timeline'>('tree');
  const [filterLLM, setFilterLLM] = useState(true);
  const [filterTool, setFilterTool] = useState(true);
  const [filterEngine, setFilterEngine] = useState(true);

  // Live Mode
  const [autoRefresh, setAutoRefresh] = useState(false);

  useEffect(() => {
    if (selectedTrace) {
      window.location.hash = selectedTrace;
    } else {
      window.location.hash = '';
    }
  }, [selectedTrace]);

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

  const fetchTraceIds = useCallback(async (showRefresh = false) => {
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
  }, [globalSearch, listHasError, listLimit, listShowDaemon]);

  const handleDeleteAll = async () => {
    if (!confirm("Are you sure you want to clear ALL traces? This cannot be undone.")) return;
    try {
      await axios.delete('/v1/traces');
      setTracesMeta([]);
      setSelectedTrace(null);
      setTraceData(null);
    } catch {
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
    } catch {
      alert("Failed to delete trace.");
    }
  };

  const loadTrace = useCallback(async (id: string, append = false) => {
    if (!append) {
      setSelectedTrace(id);
      setTraceData(null);
      setTraceOffset(0);
      setHasMoreEvents(false);
    }
    setLoading(true);
    const currentOffset = append ? traceOffset : 0;
    try {
      const res = await axios.get(`/v1/traces/${id}?limit=${EVENTS_PER_PAGE}&offset=${currentOffset}`);
      const rawData = res.data;
      const actualData = (rawData && rawData.data && (rawData.data.events || rawData.data.runs)) ? rawData.data : rawData;

      if (append && traceData) {
        // Merge runs
        const runMap = new Map<string, TraceRunView>();
        traceData.runs?.forEach((r: TraceRunView) => runMap.set(r.id, r));
        actualData.runs?.forEach((r: TraceRunView) => {
          runMap.set(r.id, r);
        });

        // Merge loop decisions
        const decisionMap = new Map<string, any>();
        traceData.loop_decisions?.forEach((d: any) => decisionMap.set(d.id, d));
        actualData.loop_decisions?.forEach((d: any) => decisionMap.set(d.id, d));

        setTraceData({
          events: [...(traceData.events || []), ...(actualData.events || [])],
          runs: Array.from(runMap.values()),
          loop_decisions: Array.from(decisionMap.values()),
          loop_runs: actualData.loop_runs || traceData.loop_runs,
          evaluations: actualData.evaluations || traceData.evaluations,
        });
      } else {
        setTraceData(actualData);
      }

      const receivedCount = (actualData.events?.length || 0);
      if (receivedCount === EVENTS_PER_PAGE) {
        setHasMoreEvents(true);
      } else {
        setHasMoreEvents(false);
      }
      setTraceOffset(currentOffset + receivedCount);

    } catch (err) {
      console.error('Failed to load trace', err);
    } finally {
      setLoading(false);
    }
  }, [traceData, traceOffset]);

  // Deep links, list refresh, and keyboard navigation share stable callbacks so
  // subscriptions always observe the current filters and pagination state.
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
  }, [loadTrace, selectedTrace]);

  useEffect(() => {
    fetchTraceIds();
    const interval = setInterval(() => {
      if (autoRefresh) {
        fetchTraceIds(false);
      }
    }, 5000);
    return () => clearInterval(interval);
  }, [autoRefresh, fetchTraceIds]);

  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (!selectedTrace || tracesMeta.length === 0) return;
      if (document.activeElement?.tagName === 'INPUT' || document.activeElement?.tagName === 'TEXTAREA') return;

      const currentIndex = tracesMeta.findIndex(m => m.trace_id === selectedTrace);
      if (currentIndex === -1) return;

      if (e.key === 'ArrowDown') {
        e.preventDefault();
        const next = tracesMeta[currentIndex + 1];
        if (next) loadTrace(next.trace_id);
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        const prev = tracesMeta[currentIndex - 1];
        if (prev) loadTrace(prev.trace_id);
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [loadTrace, selectedTrace, tracesMeta]);

  let totalDuration = 0;
  let rootRuns: TraceRunView[] = [];
  let allRuns: TraceRunView[] = [];
  let firstTime = 0;
  let totalTokensInput = 0;
  let totalTokensOutput = 0;
  let maxExclusiveTime = 0;
  let bottleneckRunId = '';
  const loopRunCount = traceData?.loop_runs?.length || 0;

  if (traceData && traceData.runs && traceData.runs.length > 0) {
    allRuns = traceData.runs;

    const runDurations = new Map<string, number>();
    const childDurations = new Map<string, number>();
    
    allRuns.forEach(r => {
      const dur = Math.max(0, r.end_time - r.start_time);
      runDurations.set(r.id, dur);
      childDurations.set(r.id, 0);
    });
    
    allRuns.forEach(r => {
      if (r.parent_run_id && childDurations.has(r.parent_run_id)) {
         childDurations.set(r.parent_run_id, childDurations.get(r.parent_run_id)! + runDurations.get(r.id)!);
      }
    });
    
    allRuns.forEach(r => {
       const dur = runDurations.get(r.id)!;
       const childDur = childDurations.get(r.id)!;
       const exclusiveTime = Math.max(0, dur - childDur);
       if (exclusiveTime > maxExclusiveTime && exclusiveTime > 0.5) {
          maxExclusiveTime = exclusiveTime;
          bottleneckRunId = r.id;
       }
    });

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
  const loopDecisionRecords = (traceData?.loop_decisions || [])
    .map((event: any) => ({ event, payload: loopDecisionPayload(event) }))
    .filter((item: any) => item.payload && Object.keys(item.payload).length > 0);
  const transitionDecisionRecords = loopDecisionRecords.filter((item: any) => Boolean(item.payload?.evidence?.condition));
  const gateDecisionRecords = loopDecisionRecords.filter((item: any) => {
    const evidence = item.payload?.evidence || {};
    return Boolean(evidence.grant || item.payload?.gate_results?.length);
  });
  const blockedLoopDecisionCount = loopDecisionRecords.filter((item: any) => {
    const decision = String(item.payload?.decision || '');
    return decision === 'blocked' || decision === 'failed';
  }).length;
  const latestBudgetState = [...gateDecisionRecords]
    .reverse()
    .map((item: any) => item.payload?.evidence?.grant?.budget_state || item.payload?.gate_results?.[0]?.evidence?.grant?.budget_state)
    .find(Boolean);
  const gateLedgerRows = gateDecisionRecords.slice(-10).reverse();
  const sideEffectRows = loopDecisionRecords
    .map((item: any) => ({ ...item, sideEffect: sideEffectFromDecision(item.payload) }))
    .filter((item: any) => item.sideEffect)
    .slice(-10)
    .reverse();
  const loopRunSummaries = (traceData?.loop_runs || []).map((detail: any) => {
    const runState = detail.run_state || {};
    const events = detail.events || [];
    const checkpoints = detail.checkpoints || [];
    const lastEvent = events.length > 0 ? events[events.length - 1] : null;
    return {
      runState,
      events,
      checkpoints,
      lastEvent,
    };
  });

  const handleReplay = async () => {
    const firstUserMsg = allRuns.find(r => r.name === 'Channel Receive')?.inputs?.message;
    const replayText = typeof firstUserMsg === 'string' ? firstUserMsg : firstUserMsg?.text || JSON.stringify(firstUserMsg);
    if (!replayText) return alert("Could not extract original User input from Channel Receive.");
    if (!confirm(`Replay original input?\n\n"${replayText}"`)) return;
    try {
      await axios.post('/v1/chat', { message: replayText });
      setTimeout(() => fetchTraceIds(true), 2000);
      alert("Replay request sent!");
    } catch { alert("Replay failed"); }
  };

  const handleEval = async () => {
    if (!selectedTrace) return;
    if (!confirm("Trigger system evaluation for this trace?")) return;
    try {
      await axios.post(`/v1/trace_evaluate?trace_id=${selectedTrace}`);
      alert("Evaluation task triggered.");
    } catch { alert("Eval failed"); }
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
              {group.traces.map((meta) => {
                const threadColor = meta.thread_id ? stringToColor(meta.thread_id) : 'transparent';
                return (
                <div
                  key={meta.trace_id}
                  className={`trace-item ${selectedTrace === meta.trace_id ? 'active' : ''}`}
                  onClick={() => loadTrace(meta.trace_id)}
                  style={meta.thread_id ? { borderLeft: `4px solid ${threadColor}`, paddingLeft: 12, borderTopLeftRadius: 2, borderBottomLeftRadius: 2 } : {}}
                >
                  <div className="trace-id" style={{ wordBreak: 'break-all', fontSize: '0.75rem', lineHeight: 1.4, color: meta.has_error ? 'var(--error-color)' : 'inherit', fontWeight: meta.has_error ? 600 : 'normal', display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between' }}>
                    <span>
                      {meta.has_error && <ShieldAlert size={12} style={{display: 'inline', marginRight: 4}}/>}
                      {meta.trace_id}
                    </span>
                    <div style={{ display: 'flex', alignItems: 'center' }}>
                      {meta.thread_id && (
                         <span style={{ fontSize: '0.65rem', padding: '2px 4px', borderRadius: 4, background: threadColor, color: '#000', whiteSpace: 'nowrap', marginLeft: 6, fontWeight: 700 }} title={`Session: ${meta.thread_id}`}>
                           SESSION
                         </span>
                      )}
                      {meta.outcome && meta.outcome !== 'success' && meta.outcome !== 'unknown' && (
                        <span style={{ fontSize: '0.65rem', padding: '2px 4px', borderRadius: 4, background: meta.outcome === 'failure' ? 'rgba(239,68,68,0.2)' : 'rgba(245,158,11,0.2)', color: meta.outcome === 'failure' ? '#fca5a5' : '#fcd34d', whiteSpace: 'nowrap', marginLeft: 6 }}>
                          {meta.failure_domain ? meta.failure_domain.replace(/_/g, ' ') : meta.outcome}
                        </span>
                      )}
                    </div>
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
              )})}
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
                    <div className="metric-box">
                      <div className="metric-label"><ListTree size={14} /> Loop Runs</div>
                      <div className="metric-value">{loopRunCount}</div>
                    </div>
                  </div>
                </div>

                {traceData.evaluations && traceData.evaluations.length > 0 && (
                  <div style={{ marginTop: 20, display: 'flex', flexDirection: 'column', gap: 10 }}>
                    <h3 style={{ fontSize: '0.85rem', textTransform: 'uppercase', color: 'var(--text-secondary)' }}>Evaluations</h3>
                    {traceData.evaluations.map((evalItem: any, idx: number) => (
                      <div key={idx} className="glass-panel" style={{ padding: 12, borderRadius: 8, borderLeft: evalItem.outcome === 'success' ? '4px solid var(--success-color)' : (evalItem.outcome === 'failure' ? '4px solid var(--error-color)' : '4px solid var(--warning-color)') }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
                          <span className="badge" style={{ background: evalItem.outcome === 'success' ? 'rgba(16,185,129,0.2)' : 'rgba(239,68,68,0.2)', color: evalItem.outcome === 'success' ? '#34d399' : '#fca5a5' }}>
                            {evalItem.outcome.toUpperCase()}
                          </span>
                          <span style={{ fontWeight: 600, fontSize: '0.9rem' }}>{evalItem.failure_domain.replace(/_/g, ' ')}</span>
                          <span style={{ marginLeft: 'auto', fontSize: '0.75rem', opacity: 0.6 }}>
                            {new Date(evalItem.created_at * 1000).toLocaleString()}
                          </span>
                        </div>
                        <CollapsibleJson title="Evaluation Evidence" jsonStr={evalItem.evidence} defaultOpen={evalItem.outcome !== 'success'} />
                      </div>
                    ))}
                  </div>
                )}

                <div style={{ marginTop: 24, display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap' }}>
                  <div className="mode-toggle glass-panel" style={{ display: 'flex', padding: 4, borderRadius: 8, background: 'rgba(0,0,0,0.3)' }}>
                    <button className={`toggle-btn ${viewMode === 'tree' ? 'active' : ''}`} onClick={() => setViewMode('tree')}>
                      <ListTree size={14} style={{marginRight: 6}}/> Tree View
                    </button>
                    <button className={`toggle-btn ${viewMode === 'chat' ? 'active' : ''}`} onClick={() => setViewMode('chat')}>
                      <MessageSquare size={14} style={{marginRight: 6}}/> Chat View
                    </button>
                    <button className={`toggle-btn ${viewMode === 'timeline' ? 'active' : ''}`} onClick={() => setViewMode('timeline')}>
                      <BarChart2 size={14} style={{marginRight: 6}}/> Timeline View
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

            {(loopRunSummaries.length > 0 || loopDecisionRecords.length > 0) && (
              <div className="glass-panel" style={{ padding: 18, borderRadius: 8, marginBottom: 24 }}>
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 16, marginBottom: 14, flexWrap: 'wrap' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <ListTree size={16} color="var(--accent-color)" />
                    <h3 style={{ margin: 0, fontSize: '0.95rem' }}>Loop Control</h3>
                  </div>
                  <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
                    <span className="badge">decisions {loopDecisionRecords.length}</span>
                    <span className="badge">transitions {transitionDecisionRecords.length}</span>
                    <span className="badge">gates {gateDecisionRecords.length}</span>
                    <span className="badge">side effects {sideEffectRows.length}</span>
                    <span className="badge" style={{ color: blockedLoopDecisionCount ? '#fca5a5' : '#34d399' }}>
                      blocked {blockedLoopDecisionCount}
                    </span>
                  </div>
                </div>

                {latestBudgetState && (
                  <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))', gap: 10, marginBottom: 16 }}>
                    <div style={{ padding: 10, border: '1px solid rgba(255,255,255,0.08)', borderRadius: 6 }}>
                      <div className="metric-label"><Database size={13} /> Calls Left</div>
                      <div className="metric-value" style={{ fontSize: '1rem' }}>{budgetValue(latestBudgetState.call_budget_remaining)}</div>
                    </div>
                    <div style={{ padding: 10, border: '1px solid rgba(255,255,255,0.08)', borderRadius: 6 }}>
                      <div className="metric-label"><Activity size={13} /> Tokens Left</div>
                      <div className="metric-value" style={{ fontSize: '1rem' }}>{budgetValue(latestBudgetState.token_budget_remaining)}</div>
                    </div>
                    <div style={{ padding: 10, border: '1px solid rgba(255,255,255,0.08)', borderRadius: 6 }}>
                      <div className="metric-label"><Timer size={13} /> Cost Left</div>
                      <div className="metric-value" style={{ fontSize: '1rem' }}>{budgetValue(latestBudgetState.cost_budget_remaining)}</div>
                    </div>
                    <div style={{ padding: 10, border: '1px solid rgba(255,255,255,0.08)', borderRadius: 6 }}>
                      <div className="metric-label"><ShieldAlert size={13} /> Gate State</div>
                      <div className="metric-value" style={{ fontSize: '1rem', color: latestBudgetState.decision === 'allow' ? '#34d399' : '#fca5a5' }}>
                        {latestBudgetState.reason || latestBudgetState.decision}
                      </div>
                    </div>
                  </div>
                )}

                {sideEffectRows.length > 0 && (
                  <div style={{ overflowX: 'auto', marginBottom: 16 }}>
                    <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.78rem' }}>
                      <thead>
                        <tr style={{ color: 'var(--text-secondary)', textAlign: 'left', borderBottom: '1px solid rgba(255,255,255,0.08)' }}>
                          <th style={{ padding: '8px 6px' }}>Time</th>
                          <th style={{ padding: '8px 6px' }}>Tool</th>
                          <th style={{ padding: '8px 6px' }}>Scope</th>
                          <th style={{ padding: '8px 6px' }}>State</th>
                          <th style={{ padding: '8px 6px' }}>Artifact</th>
                          <th style={{ padding: '8px 6px' }}>Commit</th>
                          <th style={{ padding: '8px 6px' }}>Compensate</th>
                        </tr>
                      </thead>
                      <tbody>
                        {sideEffectRows.map((item: any) => {
                          const effect = item.sideEffect || {};
                          const artifact = String(effect.artifact || '');
                          const state = String(effect.state || '');
                          const tool = item.payload?.tool || effect.action || '-';
                          return (
                            <tr key={`${item.event.id}-side-effect`} style={{ borderBottom: '1px solid rgba(255,255,255,0.05)' }}>
                              <td style={{ padding: '8px 6px', fontFamily: 'monospace' }}>{item.event.created_at ? format(new Date(item.event.created_at * 1000), 'HH:mm:ss.SSS') : '-'}</td>
                              <td style={{ padding: '8px 6px' }}>{tool}</td>
                              <td style={{ padding: '8px 6px' }}>{effect.scope || '-'}</td>
                              <td style={{ padding: '8px 6px', color: state === 'committed' ? '#34d399' : '#fbbf24' }}>{state || '-'}</td>
                              <td style={{ padding: '8px 6px', maxWidth: 240, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', fontFamily: 'monospace' }} title={artifact}>{artifact || '-'}</td>
                              <td style={{ padding: '8px 6px' }}>{effect.commit || '-'}</td>
                              <td style={{ padding: '8px 6px' }}>{effect.compensate || '-'}</td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                )}

                {loopRunSummaries.length > 0 && (
                  <div style={{ overflowX: 'auto', marginBottom: gateLedgerRows.length > 0 ? 16 : 0 }}>
                    <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.78rem' }}>
                      <thead>
                        <tr style={{ color: 'var(--text-secondary)', textAlign: 'left', borderBottom: '1px solid rgba(255,255,255,0.08)' }}>
                          <th style={{ padding: '8px 6px' }}>Loop Run</th>
                          <th style={{ padding: '8px 6px' }}>Node</th>
                          <th style={{ padding: '8px 6px' }}>Terminal</th>
                          <th style={{ padding: '8px 6px' }}>Attempt</th>
                          <th style={{ padding: '8px 6px' }}>Events</th>
                          <th style={{ padding: '8px 6px' }}>Checkpoints</th>
                          <th style={{ padding: '8px 6px' }}>Last Transition</th>
                        </tr>
                      </thead>
                      <tbody>
                        {loopRunSummaries.map((item: any) => {
                          const runId = String(item.runState.run_id || '');
                          const terminal = String(item.runState.terminal_state || 'active');
                          const lastCondition = item.lastEvent?.evidence?.condition || item.lastEvent?.event_type || '';
                          return (
                            <tr key={runId} style={{ borderBottom: '1px solid rgba(255,255,255,0.05)' }}>
                              <td style={{ padding: '8px 6px', fontFamily: 'monospace', color: '#c4b5fd' }}>{runId.slice(0, 12)}</td>
                              <td style={{ padding: '8px 6px' }}>{item.runState.node || '-'}</td>
                              <td style={{ padding: '8px 6px', color: terminal === 'converged' ? '#34d399' : (terminal === 'active' ? '#93c5fd' : '#fca5a5') }}>{terminal}</td>
                              <td style={{ padding: '8px 6px' }}>{item.runState.attempt || '-'}</td>
                              <td style={{ padding: '8px 6px' }}>{item.events.length}</td>
                              <td style={{ padding: '8px 6px' }}>{item.checkpoints.length}</td>
                              <td style={{ padding: '8px 6px' }}>{lastCondition || '-'}</td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                )}

                {gateLedgerRows.length > 0 && (
                  <div style={{ overflowX: 'auto' }}>
                    <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.78rem' }}>
                      <thead>
                        <tr style={{ color: 'var(--text-secondary)', textAlign: 'left', borderBottom: '1px solid rgba(255,255,255,0.08)' }}>
                          <th style={{ padding: '8px 6px' }}>Time</th>
                          <th style={{ padding: '8px 6px' }}>Gate</th>
                          <th style={{ padding: '8px 6px' }}>Decision</th>
                          <th style={{ padding: '8px 6px' }}>Reason</th>
                          <th style={{ padding: '8px 6px' }}>Calls Left</th>
                          <th style={{ padding: '8px 6px' }}>Tokens Left</th>
                          <th style={{ padding: '8px 6px' }}>Cost Left</th>
                        </tr>
                      </thead>
                      <tbody>
                        {gateLedgerRows.map((item: any) => {
                          const payload = item.payload || {};
                          const grant = payload.evidence?.grant || payload.gate_results?.[0]?.evidence?.grant || {};
                          const budget = grant.budget_state || {};
                          const decision = String(payload.decision || grant.decision || '');
                          return (
                            <tr key={item.event.id} style={{ borderBottom: '1px solid rgba(255,255,255,0.05)' }}>
                              <td style={{ padding: '8px 6px', fontFamily: 'monospace' }}>{item.event.created_at ? format(new Date(item.event.created_at * 1000), 'HH:mm:ss.SSS') : '-'}</td>
                              <td style={{ padding: '8px 6px' }}>{payload.tool || payload.evidence?.kind || '-'}</td>
                              <td style={{ padding: '8px 6px', color: decisionTone(decision) }}>{decision || '-'}</td>
                              <td style={{ padding: '8px 6px' }}>{grant.reason || payload.reason || '-'}</td>
                              <td style={{ padding: '8px 6px' }}>{budgetValue(budget.call_budget_remaining)}</td>
                              <td style={{ padding: '8px 6px' }}>{budgetValue(budget.token_budget_remaining)}</td>
                              <td style={{ padding: '8px 6px' }}>{budgetValue(budget.cost_budget_remaining)}</td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            )}

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

            {viewMode === 'chat' && (
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
                        <SmartMarkdown>{typeof text === 'string' ? text : JSON.stringify(text)}</SmartMarkdown>
                      </div>
                    </div>
                  );
                })}
              </div>
            )}

            {viewMode === 'timeline' && (
              <div className="timeline-view glass-panel" style={{ padding: '30px 20px 20px', marginTop: 20, borderRadius: 8, overflowX: 'auto', background: 'rgba(0,0,0,0.4)' }}>
                <div style={{ position: 'relative', width: '100%', minWidth: Math.max(1200, totalDuration * 80), minHeight: allRuns.length * 36 + 40 }}>
                  {[0, 25, 50, 75, 100].map(pct => (
                    <div key={pct} style={{ position: 'absolute', left: `${pct}%`, top: 0, bottom: 0, borderLeft: '1px dashed rgba(255,255,255,0.1)', zIndex: 0 }}>
                       <span style={{ position: 'absolute', top: -20, left: -10, fontSize: '0.65rem', color: 'var(--text-secondary)' }}>{((pct / 100) * totalDuration).toFixed(1)}s</span>
                    </div>
                  ))}
                  {allRuns.slice().sort((a,b) => a.start_time - b.start_time).map((r, idx) => {
                    const eventStart = Math.max(0, (r.start_time - firstTime));
                    const eventDuration = Math.max(0, r.end_time - r.start_time);
                    const leftPercent = totalDuration > 0 ? (eventStart / totalDuration) * 100 : 0;
                    const widthPercent = totalDuration > 0 ? Math.max(0.5, (eventDuration / totalDuration) * 100) : 100;
                    let bgColor = 'var(--accent-color)';
                    let fgColor = '#fff';
                    if (r.id === bottleneckRunId) { bgColor = '#ef4444'; fgColor = '#fff'; }
                    else if (r.status === 'error') bgColor = 'var(--error-color)';
                    else if (r.run_type === 'llm') { bgColor = '#fcd34d'; fgColor = '#000'; }
                    else if (r.run_type === 'tool') bgColor = '#60a5fa';
                    
                    return (
                      <div key={r.id} style={{ position: 'absolute', top: idx * 36 + 20, left: `${leftPercent}%`, width: `${widthPercent}%`, height: 26, background: bgColor, borderRadius: 4, opacity: 0.9, display: 'flex', alignItems: 'center', padding: '0 8px', overflow: 'hidden', whiteSpace: 'nowrap', fontSize: '0.75rem', color: fgColor, cursor: 'pointer', zIndex: 1, boxShadow: '0 2px 4px rgba(0,0,0,0.3)' }} title={`${r.name} (${eventDuration.toFixed(2)}s)${r.id === bottleneckRunId ? ' - BOTTLENECK' : ''}`}>
                        <span style={{ fontWeight: 600 }}>{r.id === bottleneckRunId ? '🔥 ' : ''}{r.name}</span>
                      </div>
                    );
                  })}
                </div>
              </div>
            )}

            {viewMode === 'tree' && (
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

            {hasMoreEvents && (
              <div style={{ textAlign: 'center', margin: '30px 0', paddingBottom: '20px' }}>
                <button
                  className="filter-btn highlight-btn"
                  onClick={() => loadTrace(selectedTrace!, true)}
                  disabled={loading}
                  style={{ background: 'rgba(59, 130, 246, 0.1)', border: '1px solid rgba(59, 130, 246, 0.3)', color: '#60a5fa', padding: '10px 24px', fontSize: '1rem', cursor: 'pointer' }}
                >
                  {loading ? 'Loading More...' : 'Load More Trace Data'}
                </button>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

export default App;
