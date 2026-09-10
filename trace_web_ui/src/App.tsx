import { useCallback, useEffect, useState } from 'react';
import axios from 'axios';
import { format } from 'date-fns';
import { Activity, Code, CheckCircle2, XCircle, Search, ChevronDown, ChevronRight, Zap, Copy, Check, RefreshCw, Timer, ShieldAlert, Layers, Inbox, Send, Download, Play, Pause, Trash2, RotateCcw, Rocket, ListTree, MessageSquare, Database, BarChart2, Terminal, Brain, Sparkles, ArrowRight } from 'lucide-react';
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

interface RunInterpretation {
  chineseTitle: string;
  category: 'input' | 'llm' | 'tool' | 'checker' | 'decision' | 'output' | 'engine';
  summary: string;
  badge?: { text: string; color: string; bg: string };
  command?: string;
  query?: string;
  formattedOutput?: string;
  isTerminal?: boolean;
  exitCode?: number;
  cwd?: string;
  draftReply?: string;
  evidenceItems?: string[];
  errorAdvice?: string;
}

const formatRelativeTime = (timestamp: number) => {
  if (!timestamp) return '';
  const now = Date.now() / 1000;
  const diff = now - timestamp;
  if (diff < 60) return '刚刚';
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  if (diff < 86400 * 2) return '昨天';
  return format(new Date(timestamp * 1000), 'MM-dd HH:mm');
};

const getChannelLabel = (meta: any) => {
  const tid = meta.thread_id || '';
  if (tid.includes('weixin') || tid.includes('wechat') || (meta.peer_id && String(meta.peer_id).includes('wechat'))) return '微信';
  if (tid.includes('cli')) return 'CLI';
  if (meta.trace_id && meta.trace_id.length === 32 && !meta.thread_id) return '后台任务';
  return 'Web/API';
};

const interpretRun = (run: TraceRunView): RunInterpretation => {
  const name = run.name || '';
  const isError = run.status === 'error';
  const isBlocked = run.status === 'blocked';

  if (name === 'Channel Receive') {
    const msg = run.inputs?.message?.text || run.inputs?.message || run.inputs?.text || run.metadata?.objective || '';
    const text = typeof msg === 'string' ? msg : JSON.stringify(msg);
    return {
      chineseTitle: '收到用户消息 (User Input)',
      category: 'input',
      summary: text ? `用户输入：“${text.slice(0, 150)}${text.length > 150 ? '...' : ''}”` : '系统接收到外部通道消息',
      badge: { text: '用户输入', color: '#a78bfa', bg: 'rgba(167, 139, 250, 0.15)' }
    };
  }

  if (name === 'Channel Send') {
    const msg = run.outputs?.message?.text || run.outputs?.message || run.inputs?.message?.text || run.inputs?.message || '';
    const text = typeof msg === 'string' ? msg : '';
    return {
      chineseTitle: '向用户发送最终回复 (Assistant Reply)',
      category: 'output',
      summary: text ? `正式交付答复：“${text.slice(0, 150)}${text.length > 150 ? '...' : ''}”` : '经过思考规划、工具调用与质检核验，向用户正式投递结果。',
      formattedOutput: text,
      badge: { text: '发送成功', color: '#34d399', bg: 'rgba(52, 211, 153, 0.15)' }
    };
  }

  if (name === 'Trace') {
    return {
      chineseTitle: '会话任务全景 (Session Trace)',
      category: 'input',
      summary: '包含本轮用户输入、多轮意图思考、工具调用与最终答复的完整事务周期。',
      badge: { text: '全景事务', color: '#a78bfa', bg: 'rgba(167, 139, 250, 0.15)' }
    };
  }

  if (name === 'Turn') {
    return {
      chineseTitle: '用户交互回合 (User Turn)',
      category: 'input',
      summary: '单次回合交互周期，从接收用户消息到最终答复送达。',
      badge: { text: '交互回合', color: '#818cf8', bg: 'rgba(129, 140, 248, 0.15)' }
    };
  }

  if (name.startsWith('Step ')) {
    const stepNum = name.replace('Step ', '').trim();
    return {
      chineseTitle: `第 ${stepNum} 轮规划推进 (Step ${stepNum})`,
      category: 'llm',
      summary: `大模型第 ${stepNum} 轮意图分析、工具调用与结果反馈。`,
      badge: { text: `Step ${stepNum}`, color: '#60a5fa', bg: 'rgba(96, 165, 250, 0.15)' }
    };
  }

  if (name === 'Planner Reasoning' || run.run_type === 'llm') {
    const sec = Math.max(0, run.end_time - run.start_time).toFixed(1);
    if (isError) {
      const err = run.outputs?.error || run.outputs?.exception || '参数校验不匹配';
      const errStr = String(err);
      let advice = '大模型生成的参数未符合结构约束，系统已自动拦截并触发自愈重试。';
      if (errStr.includes('$.command')) {
        advice = '生成的终端命令参数列表超过了 32 项限制，系统已自动触发自愈重试进行精简或拆分。';
      }
      return {
        chineseTitle: '大模型思考规划 (Planner Reasoning)',
        category: 'llm',
        summary: `⚠️ 参数校验拦截：${errStr.slice(0, 120)}。系统已捕获该问题并执行自愈重试。`,
        errorAdvice: advice,
        badge: { text: '参数校验/触发自愈', color: '#fbbf24', bg: 'rgba(251, 191, 36, 0.15)' }
      };
    }

    let parsedResponse: any = null;
    try {
      if (typeof run.outputs?.llm_response === 'string') {
        parsedResponse = JSON.parse(run.outputs.llm_response);
      } else if (run.outputs?.llm_response && typeof run.outputs.llm_response === 'object') {
        parsedResponse = run.outputs.llm_response;
      }
    } catch {}

    const plannedSyscall = parsedResponse?.syscalls?.[0] || {};
    const plannedTool = plannedSyscall.tool || run.outputs?.tool || '';
    const plannedArgs = plannedSyscall.args || run.outputs?.args || {};

    if (plannedTool === 'respond') {
      const draft = typeof plannedArgs.message === 'string' ? plannedArgs.message : '';
      return {
        chineseTitle: '大模型深度思考 ➔ 拟定答复 (Draft Response)',
        category: 'llm',
        summary: draft ? `大模型阅读上下文后，耗时 ${sec}s 拟定了初步答复：“${draft.slice(0, 120)}${draft.length > 120 ? '...' : ''}”` : `大模型耗时 ${sec}s 组织完成对用户的回复文本。`,
        draftReply: draft,
        badge: { text: `深度思考 ${sec}s`, color: '#fcd34d', bg: 'rgba(252, 211, 77, 0.15)' }
      };
    }

    if (plannedTool === 'shell.run') {
      const cmd = Array.isArray(plannedArgs.command) ? plannedArgs.command.join(' ') : String(plannedArgs.command || '');
      return {
        chineseTitle: '大模型深度思考 ➔ 规划终端执行 (Plan Shell Run)',
        category: 'llm',
        summary: `大模型分析上下文后，耗时 ${sec}s 决定通过终端命令行工具检索或操作文件。`,
        command: cmd ? `$ ${cmd}` : '',
        badge: { text: `深度思考 ${sec}s`, color: '#fcd34d', bg: 'rgba(252, 211, 77, 0.15)' }
      };
    }

    if (plannedTool === 'context.search') {
      const q = plannedArgs.query || '';
      return {
        chineseTitle: '大模型深度思考 ➔ 规划记忆检索 (Plan Context Search)',
        category: 'llm',
        summary: `大模型发现需要确凿事实凭据，耗时 ${sec}s 决定在历史会话与记忆库中搜索：“${q}”。`,
        query: String(q),
        badge: { text: `深度思考 ${sec}s`, color: '#fcd34d', bg: 'rgba(252, 211, 77, 0.15)' }
      };
    }

    return {
      chineseTitle: '大模型思考规划 (Planner Reasoning)',
      category: 'llm',
      summary: `大模型阅读上下文与事实证据，耗时 ${sec}s 深入思考，并决定接下来调用的工具。`,
      badge: { text: `深度思考 ${sec}s`, color: '#fcd34d', bg: 'rgba(252, 211, 77, 0.15)' }
    };
  }

  if (name.includes('checker') || name === 'Quality Checker') {
    const evidenceSummary = run.outputs?.evidence_summary || run.outputs?.summary || run.outputs?.facts?.evidence_summary || '';
    if (isBlocked || isError) {
      return {
        chineseTitle: '质量核验门禁 (Quality Checker)',
        category: 'checker',
        summary: evidenceSummary ? `🛡️ 质检拦截：${evidenceSummary}` : '🛡️ 质检拦截：检测到回答缺乏充分的事实凭据，质检门禁驳回草率回复，强制要求调工具验证。',
        errorAdvice: evidenceSummary || '缺少检索或记忆证据',
        badge: { text: '质检拦截 (防幻觉)', color: '#fca5a5', bg: 'rgba(239, 68, 68, 0.15)' }
      };
    }
    return {
      chineseTitle: '质量核验门禁 (Quality Checker)',
      category: 'checker',
      summary: evidenceSummary ? `✅ 质检通过：${evidenceSummary}` : '✅ 质检通过：事实证据核验达标，事实论据确凿，准许向用户交付正式回复。',
      badge: { text: '质检合格', color: '#34d399', bg: 'rgba(52, 211, 153, 0.15)' }
    };
  }

  if (name.includes('shell.run')) {
    const facts = run.outputs?.facts || {};
    const cmdArgs = facts.command || run.inputs?.args?.command || run.inputs?.command || [];
    const cmdStr = Array.isArray(cmdArgs) ? cmdArgs.join(' ') : String(cmdArgs);
    const stdout = facts.stdout || run.outputs?.stdout || facts.stderr || run.outputs?.stderr || '';
    const exitCode = facts.exit_code !== undefined ? facts.exit_code : (run.outputs?.exit_code !== undefined ? run.outputs.exit_code : 0);
    const cwd = facts.cwd || run.inputs?.args?.cwd || '';
    return {
      chineseTitle: '执行终端命令 (Shell Command)',
      category: 'tool',
      summary: exitCode === 0 ? `在系统主机上成功执行终端命令（退出码 0）。` : `终端命令执行返回异常退出码 ${exitCode}。`,
      command: cmdStr ? `$ ${cmdStr}` : '',
      formattedOutput: stdout ? String(stdout).slice(0, 1200) : '',
      isTerminal: true,
      exitCode,
      cwd,
      badge: { text: exitCode === 0 ? '终端执行成功' : `异常退出码 ${exitCode}`, color: exitCode === 0 ? '#34d399' : '#fca5a5', bg: exitCode === 0 ? 'rgba(52, 211, 153, 0.15)' : 'rgba(239, 68, 68, 0.15)' }
    };
  }

  if (name.includes('context.search')) {
    const facts = run.outputs?.facts || {};
    const q = run.inputs?.args?.query || run.inputs?.query || facts.query || '';
    const rawEvidence = facts.evidence || [];
    const evidenceList: string[] = rawEvidence.map((e: any) => typeof e === 'string' ? e : (e.content || JSON.stringify(e))).filter(Boolean);
    const count = facts.count || evidenceList.length;
    return {
      chineseTitle: '检索聊天记录与记忆库 (Memory Search)',
      category: 'tool',
      query: String(q),
      evidenceItems: evidenceList,
      summary: count > 0 ? `在历史会话数据库和长期记忆库中命中 ${count} 条相关事实记录（检索词：“${q}”）。` : `在历史会话数据库中检索完成（检索词：“${q}”）。`,
      badge: { text: count > 0 ? `命中 ${count} 条记忆` : '记忆检索', color: '#38bdf8', bg: 'rgba(56, 189, 248, 0.15)' }
    };
  }

  if (name.includes('respond')) {
    const msg = run.inputs?.args?.message || run.inputs?.message || '';
    const draftText = typeof msg === 'string' ? msg : '';
    return {
      chineseTitle: '拟定用户答复 (Draft Response)',
      category: 'tool',
      summary: draftText ? `拟定答复：“${draftText.slice(0, 150)}${draftText.length > 150 ? '...' : ''}”` : '大模型已组织完成回复文本，提交给质检门禁核验。',
      draftReply: draftText,
      formattedOutput: draftText,
      badge: { text: '拟定回复', color: '#818cf8', bg: 'rgba(129, 140, 248, 0.15)' }
    };
  }

  if (name.startsWith('Decision:')) {
    const dec = name.replace('Decision:', '').trim();
    if (dec === 'converged') {
      return {
        chineseTitle: '系统调度：目标达成 (Converged)',
        category: 'decision',
        summary: '质检核验全部达标，系统判定本轮对话目标圆满达成，执行收敛结单。',
        badge: { text: '目标达成', color: '#34d399', bg: 'rgba(52, 211, 153, 0.15)' }
      };
    }
    if (dec === 'recover') {
      return {
        chineseTitle: '系统调度：自主自愈重试 (Self-Healing Recovery)',
        category: 'decision',
        summary: '上一环节未达标或遇到报错，调度中枢自动触发自愈机制，指导大模型修正策略。',
        badge: { text: '自愈纠错', color: '#fbbf24', bg: 'rgba(251, 191, 36, 0.15)' }
      };
    }
    return {
      chineseTitle: `系统调度：流程推进 (${dec})`,
      category: 'decision',
      summary: '当前步骤处理完毕，调度中枢推动状态机进入下一环节。',
      badge: { text: '调度推进', color: '#93c5fd', bg: 'rgba(147, 197, 253, 0.15)' }
    };
  }

  if (run.run_type === 'tool') {
    return {
      chineseTitle: `执行工具：${name.replace('Tool: ', '')}`,
      category: 'tool',
      summary: '系统代理大模型执行具体业务工具。',
      badge: { text: '工具调用', color: '#60a5fa', bg: 'rgba(96, 165, 250, 0.15)' }
    };
  }

  if (run.run_type === 'engine' || name.startsWith('Loop')) {
    return {
      chineseTitle: `引擎内部存档：${name}`,
      category: 'engine',
      summary: 'Navi 持久化运行时状态存档与心跳，用于断电恢复与分布式调度。',
      badge: { text: '内部心跳', color: '#9ca3af', bg: 'rgba(156, 163, 175, 0.15)' }
    };
  }

  return {
    chineseTitle: name,
    category: 'engine',
    summary: '执行步骤环节',
  };
};

const HumanInterpretCard = ({ run }: { run: TraceRunView }) => {
  const info = interpretRun(run);
  return (
    <div className="human-interpret-box">
      <div className="human-interpret-header">
        <div className="human-interpret-title">
          <Sparkles size={14} color="var(--accent-color)" />
          <span>{info.chineseTitle}</span>
        </div>
        {info.badge && (
          <span style={{ fontSize: '0.7rem', padding: '2px 8px', borderRadius: 4, background: info.badge.bg, color: info.badge.color, fontWeight: 600 }}>
            {info.badge.text}
          </span>
        )}
      </div>

      <div className="human-interpret-summary">
        {info.summary}
      </div>

      {info.errorAdvice && (
        <div style={{ marginTop: 8, background: 'rgba(239, 68, 68, 0.1)', border: '1px solid rgba(239, 68, 68, 0.25)', borderRadius: 6, padding: '8px 12px', fontSize: '0.8rem', color: '#fca5a5', lineHeight: 1.4 }}>
          <strong>💡 诊断排查：</strong>{info.errorAdvice}
        </div>
      )}

      {info.draftReply && (
        <div className="human-draft-box">
          <div className="human-draft-header">
            <MessageSquare size={14} /> <span>拟定的答复草稿</span>
          </div>
          <div className="markdown-body" style={{ fontSize: '0.85rem' }}>
            <SmartMarkdown>{info.draftReply}</SmartMarkdown>
          </div>
        </div>
      )}

      {info.evidenceItems && info.evidenceItems.length > 0 && (
        <div className="human-memory-box">
          <div className="human-memory-header">
            <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              <Database size={14} />
              <span>命中相关记忆与事实依据 ({info.evidenceItems.length} 条)</span>
            </div>
            {info.query && <span style={{ opacity: 0.7, fontSize: '0.72rem' }}>关键词: {info.query}</span>}
          </div>
          {info.evidenceItems.slice(0, 3).map((item, idx) => (
            <div key={idx} className="human-memory-item">
              <SmartMarkdown>{item}</SmartMarkdown>
            </div>
          ))}
          {info.evidenceItems.length > 3 && (
            <div style={{ fontSize: '0.72rem', color: '#38bdf8', marginTop: 4 }}>
              共检索出 {info.evidenceItems.length} 条记忆片段（可在下方 Result 原始数据中查看全部）
            </div>
          )}
        </div>
      )}

      {info.isTerminal && info.command && (
        <div style={{ marginTop: 8 }}>
          <div className="terminal-header-bar">
            <div className="terminal-dots">
              <div className="terminal-dot red" />
              <div className="terminal-dot yellow" />
              <div className="terminal-dot green" />
            </div>
            <div style={{ fontSize: '0.72rem', opacity: 0.6, fontFamily: 'monospace' }}>
              {info.cwd ? info.cwd.split('/').slice(-2).join('/') : 'bash'}
            </div>
            {info.exitCode !== undefined && (
              <span style={{ fontSize: '0.68rem', padding: '1px 6px', borderRadius: 4, background: info.exitCode === 0 ? 'rgba(52, 211, 153, 0.2)' : 'rgba(239, 68, 68, 0.2)', color: info.exitCode === 0 ? '#34d399' : '#fca5a5', fontFamily: 'monospace' }}>
                exit {info.exitCode}
              </span>
            )}
          </div>
          <div className="human-terminal-command" style={{ borderTopLeftRadius: 0, borderTopRightRadius: 0 }}>
            {info.command}
          </div>
          {info.formattedOutput && (
            <div className="human-terminal-output" style={{ marginTop: 4 }}>
              {info.formattedOutput}
            </div>
          )}
        </div>
      )}
    </div>
  );
};

const ExecutiveSummaryCard = ({
  traceData,
  allRuns
}: {
  traceData: any;
  allRuns: TraceRunView[];
}) => {
  const inputRun = allRuns.find(r => r.name === 'Channel Receive');
  const userPrompt = inputRun?.inputs?.message?.text || inputRun?.inputs?.message || inputRun?.inputs?.text || inputRun?.metadata?.objective || '';

  const sendRun = allRuns.find(r => r.name === 'Channel Send');
  const finalReply = sendRun?.outputs?.message?.text || sendRun?.outputs?.message || sendRun?.inputs?.message?.text || sendRun?.inputs?.message || '';

  const llmRuns = allRuns.filter(r => r.run_type === 'llm');
  const toolRuns = allRuns.filter(r => r.run_type === 'tool');
  const recoverDecisions = allRuns.filter(r => r.name === 'Decision: recover');
  const blockedCheckers = allRuns.filter(r => (r.name.includes('checker') || r.name === 'Quality Checker') && (r.status === 'blocked' || r.status === 'error'));

  const evaluations = traceData.evaluations || [];
  const primaryEval = evaluations[0] || {};
  const outcome = primaryEval.outcome || (traceData.meta?.outcome || 'success');
  const failureDomain = primaryEval.failure_domain || traceData.meta?.failure_domain || 'none';

  let diagInsight = '';
  let diagClass = 'success';
  if (outcome === 'success') {
    if (recoverDecisions.length > 0) {
      diagClass = 'degraded';
      diagInsight = `⚡ 自主修复达标：系统在规划过程中经历了 ${recoverDecisions.length} 次自愈重试，最终成功核验并向用户交付了正式答复。`;
    } else {
      diagClass = 'success';
      diagInsight = '🎯 执行通畅：大模型思考规划、工具调用与事实凭据核验均一次性合格，回答具备确凿依据。';
    }
  } else if (outcome === 'degraded') {
    diagClass = 'degraded';
    if (failureDomain === 'planner_or_parser') {
      diagInsight = '🟡 模型规划自愈：大模型生成的参数一度触发了结构校验限制（如命令行项数超限或缺少必填字段），系统拦截后重新规划并完成了修复。';
    } else if (failureDomain === 'capability_failure') {
      diagInsight = '🟡 工具自愈：某项工具调用遇到执行异常，系统捕获后调整了调用方式并继续完成。';
    } else {
      diagInsight = '🟡 降级处理：任务执行中经历自愈调节，已尽量满足用户诉求。';
    }
  } else {
    diagClass = 'failure';
    diagInsight = `🔴 任务未收敛或异常（${failureDomain}）：本轮未达成终态或缺少充分依据。建议检查模型规划参数或工具输入格式。`;
  }

  if (blockedCheckers.length > 0) {
    diagInsight += ` 防幻觉质检：门禁系统拦截了 ${blockedCheckers.length} 次无依据回答，强制补充了事实证据。`;
  }

  return (
    <div className="exec-summary-card">
      <div className="exec-summary-header">
        <div className="exec-summary-title">
          <Sparkles size={18} color="var(--accent-color)" />
          <span>会话任务智能诊断简报 (Executive Summary & Diagnosis)</span>
        </div>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          <span style={{
            padding: '3px 10px',
            borderRadius: 6,
            fontSize: '0.75rem',
            fontWeight: 700,
            background: outcome === 'success' ? 'rgba(52, 211, 153, 0.15)' : (outcome === 'degraded' ? 'rgba(251, 191, 36, 0.15)' : 'rgba(239, 68, 68, 0.15)'),
            color: outcome === 'success' ? '#34d399' : (outcome === 'degraded' ? '#fbbf24' : '#fca5a5'),
            border: `1px solid ${outcome === 'success' ? 'rgba(52, 211, 153, 0.3)' : (outcome === 'degraded' ? 'rgba(251, 191, 36, 0.3)' : 'rgba(239, 68, 68, 0.3)')}`
          }}>
            {outcome === 'success' ? '✔ 目标圆满达成' : (outcome === 'degraded' ? '⚡ 触发自愈纠错交付' : '✖ 执行异常未收敛')}
          </span>
        </div>
      </div>

      {userPrompt && (
        <div className="exec-prompt-box">
          <div className="exec-prompt-header">
            <Inbox size={14} />
            <span>用户核心诉求 (User Objective)</span>
          </div>
          <div className="exec-prompt-text">
            “{typeof userPrompt === 'string' ? userPrompt : JSON.stringify(userPrompt)}”
          </div>
        </div>
      )}

      {finalReply && (
        <div className="exec-response-box">
          <div className="exec-response-header">
            <Send size={14} />
            <span>向用户交付的最终答复 (Final Response)</span>
          </div>
          <div className="markdown-body" style={{ fontSize: '0.92rem', maxHeight: 220, overflowY: 'auto' }}>
            <SmartMarkdown>{typeof finalReply === 'string' ? finalReply : JSON.stringify(finalReply)}</SmartMarkdown>
          </div>
        </div>
      )}

      <div className="exec-stats-row">
        <div className="exec-stat-pill">
          <Brain size={14} color="#fcd34d" />
          <span>思考 {llmRuns.length} 轮</span>
        </div>
        <div className="exec-stat-pill">
          <Terminal size={14} color="#60a5fa" />
          <span>执行工具 {toolRuns.length} 次</span>
        </div>
        {recoverDecisions.length > 0 && (
          <div className="exec-stat-pill" style={{ borderColor: 'rgba(251, 191, 36, 0.3)', color: '#fbbf24' }}>
            <RotateCcw size={14} />
            <span>自愈重试 {recoverDecisions.length} 次</span>
          </div>
        )}
        {blockedCheckers.length > 0 && (
          <div className="exec-stat-pill" style={{ borderColor: 'rgba(239, 68, 68, 0.3)', color: '#fca5a5' }}>
            <ShieldAlert size={14} />
            <span>门禁拦截 {blockedCheckers.length} 次</span>
          </div>
        )}
      </div>

      <div className={`diag-insight-box ${diagClass}`}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontWeight: 700 }}>
          <Sparkles size={14} />
          <span>智能诊断与洞察 (Diagnostic Insight)</span>
        </div>
        <div>{diagInsight}</div>
      </div>
    </div>
  );
};

const StorylineBanner = ({ allRuns }: { allRuns: TraceRunView[] }) => {
  const milestones: {
    id: string;
    icon: React.ReactNode;
    label: string;
    desc: string;
    duration?: string;
    status: 'success' | 'error' | 'blocked' | 'normal';
  }[] = [];

  const inputRun = allRuns.find(r => r.name === 'Channel Receive');
  if (inputRun) {
    const msg = inputRun.inputs?.message?.text || inputRun.inputs?.message || inputRun.inputs?.text || inputRun.metadata?.objective || '';
    const text = typeof msg === 'string' ? msg : '';
    milestones.push({
      id: inputRun.id,
      icon: <Inbox size={14} color="#a78bfa" />,
      label: '收到输入',
      desc: text ? text.slice(0, 30) : '通道消息接入',
      status: 'normal',
    });
  }

  const significant = allRuns.filter(r => {
    if (r.run_type === 'llm') return true;
    if (r.run_type === 'tool') return true;
    if (r.name.includes('checker')) return true;
    if (r.name === 'Decision: recover') return true;
    return false;
  }).sort((a,b) => a.start_time - b.start_time);

  significant.forEach(r => {
    const dur = Math.max(0, r.end_time - r.start_time);
    const durStr = dur > 0 ? (dur < 1 ? `${Math.round(dur*1000)}ms` : `${dur.toFixed(1)}s`) : undefined;
    const isErr = r.status === 'error';
    const isBlk = r.status === 'blocked';

    if (r.name === 'Planner Reasoning' || r.run_type === 'llm') {
      if (isErr) {
        milestones.push({
          id: r.id,
          icon: <XCircle size={14} color="#fca5a5" />,
          label: '规划异常 (自愈)',
          desc: '参数超出限制，触发自愈重试',
          duration: durStr,
          status: 'error',
        });
      } else {
        milestones.push({
          id: r.id,
          icon: <Brain size={14} color="#fcd34d" />,
          label: '思维规划',
          desc: '大模型思考并决定调用工具',
          duration: durStr,
          status: 'normal',
        });
      }
    } else if (r.name.includes('checker')) {
      if (isBlk || isErr) {
        milestones.push({
          id: r.id,
          icon: <ShieldAlert size={14} color="#fca5a5" />,
          label: '质检拦截',
          desc: '缺乏充分事实证据，驳回回答',
          status: 'blocked',
        });
      } else {
        milestones.push({
          id: r.id,
          icon: <CheckCircle2 size={14} color="#34d399" />,
          label: '质检通过',
          desc: '事实证据核验合格',
          status: 'success',
        });
      }
    } else if (r.name === 'Decision: recover') {
      milestones.push({
        id: r.id,
        icon: <RotateCcw size={14} color="#fbbf24" />,
        label: '自愈重试',
        desc: '调度中枢重新组织策略',
        status: 'normal',
      });
    } else if (r.run_type === 'tool') {
      const toolName = r.name.replace('Tool: ', '');
      let desc = '执行工具动作';
      if (toolName === 'shell.run') desc = '终端文件搜索/执行';
      if (toolName === 'context.search') desc = '检索聊天与记忆库';
      if (toolName === 'respond') desc = '拟定最终回答';
      milestones.push({
        id: r.id,
        icon: <Terminal size={14} color="#60a5fa" />,
        label: toolName,
        desc,
        duration: durStr,
        status: isErr ? 'error' : 'success',
      });
    }
  });

  const sendRun = allRuns.find(r => r.name === 'Channel Send');
  if (sendRun) {
    milestones.push({
      id: sendRun.id,
      icon: <Send size={14} color="#34d399" />,
      label: '回复送达',
      desc: '消息正式发送给用户',
      status: 'success',
    });
  }

  if (milestones.length === 0) return null;

  return (
    <div className="storyline-card">
      <div className="storyline-header">
        <div className="storyline-title">
          <Sparkles size={16} color="var(--accent-color)" />
          <span>业务执行故事线 (Execution Storyline)</span>
          <span style={{ fontSize: '0.75rem', opacity: 0.6, fontWeight: 400, marginLeft: 8 }}>
            共 {milestones.length} 个关键里程碑环节
          </span>
        </div>
      </div>
      <div className="storyline-steps">
        {milestones.map((m, idx) => (
          <div key={`${m.id}-${idx}`} className="storyline-step-item">
            <div className={`storyline-step-box ${m.status}`} title={m.desc}>
              <div className="storyline-step-top">
                <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                  {m.icon}
                  <span className="storyline-step-label">{idx + 1}. {m.label}</span>
                </div>
                {m.duration && (
                  <span style={{ fontSize: '0.68rem', opacity: 0.7, fontFamily: 'monospace' }}>{m.duration}</span>
                )}
              </div>
              <div className="storyline-step-desc">{m.desc}</div>
            </div>
            {idx < milestones.length - 1 && (
              <div className="storyline-step-arrow">
                <ArrowRight size={14} />
              </div>
            )}
          </div>
        ))}
      </div>
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
  const humanInfo = interpretRun(run);

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

      let parsedResponse: any = null;
      try {
        if (typeof outputs.llm_response === 'string') {
          parsedResponse = JSON.parse(outputs.llm_response);
        } else if (outputs.llm_response && typeof outputs.llm_response === 'object') {
          parsedResponse = outputs.llm_response;
        }
      } catch {}

      const plannedSyscall = parsedResponse?.syscalls?.[0] || {};
      const plannedTool = plannedSyscall.tool || outputs.tool || '';
      const plannedArgs = plannedSyscall.args || outputs.args || {};
      const plannedThought = parsedResponse?.thought || parsedResponse?.reasoning || outputs.reason || '';

      runContent = (
        <div className="run-details">
          {outputs.error && (
            <div style={{ background: 'rgba(239, 68, 68, 0.1)', border: '1px solid rgba(239, 68, 68, 0.3)', borderRadius: 6, padding: '10px 14px', marginBottom: 8, color: '#fca5a5' }}>
              <div style={{ fontWeight: 600, fontSize: '0.82rem', marginBottom: 4 }}>⚠️ 参数校验异常并已触发自愈</div>
              <div style={{ fontSize: '0.8rem', opacity: 0.9 }}>{String(outputs.error)}</div>
            </div>
          )}
          {plannedThought && (
            <div style={{ background: 'rgba(252, 211, 77, 0.08)', border: '1px solid rgba(252, 211, 77, 0.25)', borderRadius: 6, padding: '10px 14px', marginBottom: 8 }}>
              <div style={{ fontWeight: 600, fontSize: '0.78rem', color: '#fcd34d', marginBottom: 4, display: 'flex', alignItems: 'center', gap: 6 }}>
                <Brain size={14} /> <span>大模型思考思路 (Thought)</span>
              </div>
              <div className="markdown-body" style={{ fontSize: '0.85rem' }}>
                <SmartMarkdown>{plannedThought}</SmartMarkdown>
              </div>
            </div>
          )}
          {plannedTool && (
            <div style={{ background: 'rgba(96, 165, 250, 0.08)', border: '1px solid rgba(96, 165, 250, 0.25)', borderRadius: 6, padding: '10px 14px', marginBottom: 8 }}>
              <div style={{ fontWeight: 600, fontSize: '0.78rem', color: '#60a5fa', marginBottom: 4, display: 'flex', alignItems: 'center', gap: 6 }}>
                <Sparkles size={14} /> <span>决定调用的动作：{plannedTool}</span>
              </div>
              {plannedTool === 'respond' && plannedArgs.message && (
                <div className="markdown-body" style={{ fontSize: '0.85rem', color: '#e5e7eb' }}>
                  <SmartMarkdown>{typeof plannedArgs.message === 'string' ? plannedArgs.message : JSON.stringify(plannedArgs.message)}</SmartMarkdown>
                </div>
              )}
              {plannedTool === 'shell.run' && plannedArgs.command && (
                <div className="human-terminal-command" style={{ marginTop: 4 }}>
                  $ {Array.isArray(plannedArgs.command) ? plannedArgs.command.join(' ') : String(plannedArgs.command)}
                </div>
              )}
              {plannedTool === 'context.search' && plannedArgs.query && (
                <div style={{ fontSize: '0.82rem', color: '#38bdf8', marginTop: 4 }}>
                  检索关键词：{plannedArgs.query}
                </div>
              )}
            </div>
          )}
          {prompt && (
            <CollapsibleJson title="Prompt 上下文" jsonStr={prompt} defaultOpen={false} />
          )}
          <CollapsibleJson title="原始模型 JSON 报文" jsonStr={outputs} defaultOpen={false} />
        </div>
      );
    } else if (run.run_type === 'tool') {
      const args = run.inputs?.args || run.inputs;
      const result = run.outputs;
      runContent = (
        <div className="run-details">
          <CollapsibleJson title="Arguments (输入参数)" jsonStr={args} defaultOpen={false} />
          <CollapsibleJson title="Result (执行结果)" jsonStr={result} defaultOpen={isError} />
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
                  {humanInfo.badge && (
                    <span style={{ marginRight: 8, fontSize: '0.65rem', fontWeight: 600, padding: '2px 6px', borderRadius: 4, background: humanInfo.badge.bg, color: humanInfo.badge.color, border: `1px solid ${humanInfo.badge.color}40`, letterSpacing: '0.02em' }}>
                      {humanInfo.badge.text}
                    </span>
                  )}
                  <span style={{ fontWeight: 600, marginRight: 6 }}>{humanInfo.chineseTitle}</span>
                  <span style={{ fontSize: '0.72rem', opacity: 0.5, fontFamily: 'monospace' }}>({run.name})</span>
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
          <HumanInterpretCard run={run} />
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
  const [filterEngine, setFilterEngine] = useState(false);
  const [showLoopControl, setShowLoopControl] = useState(false);

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
                  <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 6 }}>
                    <div style={{
                      fontSize: '0.86rem',
                      fontWeight: 600,
                      color: meta.has_error ? '#fca5a5' : '#f3f4f6',
                      lineHeight: 1.35,
                      flex: 1,
                      display: '-webkit-box',
                      WebkitLineClamp: 2,
                      WebkitBoxOrient: 'vertical',
                      overflow: 'hidden'
                    }} title={meta.preview_text || meta.trace_id}>
                      {meta.preview_text ? (
                        <span>💬 {meta.preview_text}</span>
                      ) : (
                        <span style={{ fontFamily: 'monospace', opacity: 0.8 }}>⚡ {meta.trace_id.slice(0, 16)}...</span>
                      )}
                    </div>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 4, flexShrink: 0 }}>
                      <span className="channel-tag">{getChannelLabel(meta)}</span>
                      <span style={{
                        fontSize: '0.65rem',
                        padding: '2px 5px',
                        borderRadius: 4,
                        background: meta.has_error ? 'rgba(239,68,68,0.2)' : (meta.outcome === 'degraded' ? 'rgba(245,158,11,0.2)' : 'rgba(16,185,129,0.18)'),
                        color: meta.has_error ? '#fca5a5' : (meta.outcome === 'degraded' ? '#fbbf24' : '#34d399'),
                        fontWeight: 600,
                        whiteSpace: 'nowrap'
                      }}>
                        {meta.has_error ? '自愈/重试' : (meta.outcome === 'degraded' ? '自愈完成' : '成功')}
                      </span>
                    </div>
                  </div>
                  <div className="trace-date" style={{ marginTop: 4, display: 'flex', justifyContent: 'space-between', fontSize: '0.72rem' }}>
                    <span style={{ opacity: 0.7 }}>
                      {formatRelativeTime(meta.start_time)}
                    </span>
                    <span style={{ fontFamily: 'monospace', opacity: 0.5 }}>
                      ID: {meta.trace_id.substring(0, 8)}...
                    </span>
                    {meta.duration > 0 && (
                      <span style={{ opacity: 0.8, fontFamily: 'monospace' }}>
                        <Timer size={11} style={{ display: 'inline', marginRight: 2 }} />
                        {meta.duration.toFixed(1)}s
                      </span>
                    )}
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

            {/* 0. Executive Summary & Smart Diagnosis */}
            <ExecutiveSummaryCard traceData={traceData} allRuns={allRuns} />

            {/* 1. Human Storyline Banner */}
            <StorylineBanner allRuns={allRuns} />

            {/* 2. Collapsible Engine Internals (Loop Control & Budgets) */}
            {(loopRunSummaries.length > 0 || loopDecisionRecords.length > 0) && (
              <div className="glass-panel" style={{ padding: 14, borderRadius: 8, marginBottom: 24 }}>
                <div
                  style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 16, cursor: 'pointer' }}
                  onClick={() => setShowLoopControl(!showLoopControl)}
                >
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                    <ListTree size={16} color="var(--accent-color)" />
                    <h3 style={{ margin: 0, fontSize: '0.95rem' }}>⚙️ 底层引擎状态与调度调试 (Engine Internals)</h3>
                    <span style={{ fontSize: '0.72rem', opacity: 0.6 }}>
                      {showLoopControl ? '（点击收起底层数据）' : '（点击展开查看调度转移与预算详情）'}
                    </span>
                  </div>
                  <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                    <span className="badge">decisions {loopDecisionRecords.length}</span>
                    <span className="badge">transitions {transitionDecisionRecords.length}</span>
                    <span className="badge">gates {gateDecisionRecords.length}</span>
                    <span className="badge" style={{ color: blockedLoopDecisionCount ? '#fca5a5' : '#34d399' }}>
                      blocked {blockedLoopDecisionCount}
                    </span>
                    {showLoopControl ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
                  </div>
                </div>

                {showLoopControl && (
                  <div style={{ marginTop: 16 }}>

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
                {(() => {
                  const inputRun = allRuns.find(r => r.name === 'Channel Receive');
                  const userText = inputRun?.inputs?.message?.text || inputRun?.inputs?.message || inputRun?.inputs?.text || inputRun?.metadata?.objective || '';

                  const sendRun = allRuns.find(r => r.name === 'Channel Send');
                  const botText = sendRun?.outputs?.message?.text || sendRun?.outputs?.message || sendRun?.inputs?.message?.text || sendRun?.inputs?.message || '';

                  const thoughtAndToolRuns = allRuns.filter(r => {
                    if (r.name === 'Channel Receive' || r.name === 'Channel Send' || r.name === 'Trace' || r.name === 'Turn') return false;
                    if (r.run_type === 'llm' || r.run_type === 'tool' || r.name.includes('checker') || r.name.includes('Decision: recover')) return true;
                    return false;
                  }).sort((a,b) => a.start_time - b.start_time);

                  return (
                    <>
                      {/* User Bubble */}
                      {userText && (
                        <div style={{ alignSelf: 'flex-end', maxWidth: '80%', background: 'rgba(139, 92, 246, 0.2)', border: '1px solid rgba(139, 92, 246, 0.3)', padding: '12px 16px', borderRadius: 12, borderBottomRightRadius: 0 }}>
                          <div style={{ fontSize: '0.75rem', opacity: 0.6, marginBottom: 6, textTransform: 'uppercase', display: 'flex', alignItems: 'center', gap: 6 }}>
                            <Inbox size={12} />
                            <span>User (用户提问)</span>
                          </div>
                          <div className="markdown-body" style={{ fontSize: '0.95rem' }}>
                            <SmartMarkdown>{typeof userText === 'string' ? userText : JSON.stringify(userText)}</SmartMarkdown>
                          </div>
                        </div>
                      )}

                      {/* Intermediate Agent Thinking & Action Flow */}
                      {thoughtAndToolRuns.length > 0 && (
                        <div style={{ alignSelf: 'center', width: '90%', margin: '8px 0' }}>
                          <details open style={{ background: 'rgba(255, 255, 255, 0.03)', border: '1px solid rgba(255, 255, 255, 0.08)', borderRadius: 8, padding: '12px 16px' }}>
                            <summary style={{ cursor: 'pointer', fontSize: '0.85rem', fontWeight: 600, color: 'var(--text-secondary)', display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                                <Brain size={15} color="#fcd34d" />
                                <span>Agent 思考与行动过程 ({thoughtAndToolRuns.length} 个环节)</span>
                              </div>
                              <span style={{ fontSize: '0.72rem', opacity: 0.6 }}>点击折叠/展开全部</span>
                            </summary>
                            <div style={{ marginTop: 12, display: 'flex', flexDirection: 'column', gap: 8 }}>
                              {thoughtAndToolRuns.map((r, idx) => (
                                <div key={idx} style={{ margin: '2px 0' }}>
                                  <HumanInterpretCard run={r} />
                                </div>
                              ))}
                            </div>
                          </details>
                        </div>
                      )}

                      {/* Assistant Bubble */}
                      {botText && (
                        <div style={{ alignSelf: 'flex-start', maxWidth: '85%', background: 'rgba(0,0,0,0.3)', border: '1px solid rgba(255,255,255,0.1)', padding: '14px 18px', borderRadius: 12, borderBottomLeftRadius: 0 }}>
                          <div style={{ fontSize: '0.75rem', opacity: 0.6, marginBottom: 6, textTransform: 'uppercase', display: 'flex', alignItems: 'center', gap: 6 }}>
                            <Send size={12} />
                            <span>Navi Assistant (机器人答复)</span>
                          </div>
                          <div className="markdown-body" style={{ fontSize: '0.95rem' }}>
                            <SmartMarkdown>{typeof botText === 'string' ? botText : JSON.stringify(botText)}</SmartMarkdown>
                          </div>
                        </div>
                      )}
                    </>
                  );
                })()}
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
