#!/usr/bin/env python3
import os
import sys
import re

# 定義 41 個 skills 的中英文關鍵字（核心作用驗證）
SKILLS_KEYWORDS = {
    'ai-slop-cleaner': ['slop', 'clean', '廢料', '垃圾', '清理', '代碼', '程式碼'],
    'ask': ['ask', 'advisor', 'consult', '詢問', '諮詢', '引導', '顧問'],
    'autopilot': ['autopilot', 'autonomous', '自動', '自主', '主線'],
    'autoresearch': ['research', 'improvement', '研究', '探索', '調研'],
    'cancel': ['cancel', 'abort', '取消', '終止', '中斷'],
    'ccg': ['ccg', 'tri-model', 'tri', '三模', '合成', '協調'],
    'configure-notifications': ['notification', 'telegram', 'discord', 'slack', '通知', '設定'],
    'debug': ['debug', 'diagnose', '除錯', '診斷', '定位'],
    'deep-dive': ['deep-dive', 'dive', '深入', '探討', '分析'],
    'deep-interview': ['interview', 'socratic', '訪談', '問答', '蘇格拉底', '需求'],
    'deepinit': ['deepinit', 'init', '初始化', '構建', '目錄'],
    'external-context': ['context', 'search', '外部', '上下文', '搜尋', '檢索'],
    'hud': ['hud', 'display', 'statusline', '顯示', '狀態', '抬頭'],
    'learner': ['learner', 'learn', '學習', '提取', '沉澱'],
    'local-build-reminder': ['reminder', 'rebuild', '提醒', '重建', '編譯', 'fork'],
    'mcp-setup': ['mcp', 'setup', 'mcp-setup', '伺服器', '協議'],
    'merge-readiness': ['merge', 'readiness', '合併', '準備', '測驗'],
    'omc-doctor': ['doctor', 'diagnose', '醫生', '診斷', '修復'],
    'omc-reference': ['reference', 'catalog', '參考', '手冊', '速查'],
    'omc-setup': ['setup', 'install', '安裝', '設定', '配置'],
    'omc-teams': ['team', 'tmux', '團隊', '協作', '多進程'],
    'plan': ['plan', 'strategic', '計劃', '規劃', '設計'],
    'project-session-manager': ['session', 'worktree', '會話', '工作區', '專案'],
    'ralph': ['ralph', 'loop', '迴圈', '持續', '迭代'],
    'ralplan': ['ralplan', 'consensus', '共識', '審查', '提案'],
    'release': ['release', 'publish', '發布', '版本', '規則'],
    'remember': ['remember', 'memory', '記憶', '記住', '筆記'],
    'sciomc': ['sciomc', 'scientist', '科學', '實驗', '研究'],
    'self-improve': ['improve', 'evolutionary', '演化', '自優化', '突變'],
    'setup': ['setup', 'routing', '設定', '引導', '安裝'],
    'skill': ['skill', 'manage', '技能', '管理', '精通'],
    'skillify': ['skillify', 'workflow', '技能化', '工作流', '沉澱'],
    'team': ['team', 'coordinate', '團隊', '協作', '分工'],
    'trace': ['trace', 'evidence', '追蹤', '軌跡', '因果'],
    'ultragoal': ['goal', 'durable', '目標', '持久', '任務'],
    'ultraqa': ['qa', 'testing', '測試', '品質', '修復'],
    'ultrawork': ['work', 'parallel', '工作', '平行', '吞吐'],
    'verify': ['verify', 'proof', '驗證', '證實', '確信'],
    'visual-verdict': ['visual', 'screenshot', '視覺', '截圖', '比對'],
    'wiki': ['wiki', 'knowledge', '百科', '知識', '累積'],
    'writer-memory': ['writer', 'memory', '寫作', '記憶', '角色']
}

# 定義 19 個 agents 的中英文關鍵字（核心作用驗證）
AGENTS_KEYWORDS = {
    'analyst': ['analyst', 'analysis', '分析', '需求', '解讀'],
    'architect': ['architect', 'architecture', '架構', '設計', '唯讀'],
    'code-reviewer': ['reviewer', 'review', '審查', '程式碼', '代碼'],
    'code-simplifier': ['simplifier', 'simplify', '簡化', '重構', '精簡'],
    'critic': ['critic', 'critique', '評論', '批判', '計畫'],
    'debugger': ['debugger', 'debug', '除錯', '定位', '崩潰'],
    'designer': ['designer', 'design', '設計', '介面', '視覺'],
    'document-specialist': ['document', 'specialist', '文件', '資料', '參考'],
    'executor': ['executor', 'execute', '執行', '實作', '編寫'],
    'explore': ['explore', 'search', '探索', '搜尋', '查找'],
    'git-master': ['git', 'commit', '提交', '版本', '歷史'],
    'planner': ['planner', 'plan', '規劃', '計劃', '訪談'],
    'qa-tester': ['qa', 'tester', '測試', '品質', '互動'],
    'scientist': ['scientist', 'science', '科學', '實驗', '研究'],
    'security-reviewer': ['security', 'vulnerability', '安全', '漏洞', '防禦'],
    'test-engineer': ['test', 'engineer', '測試', '案例', '覆蓋率'],
    'tracer': ['tracer', 'trace', '追蹤', '因果', '假說'],
    'verifier': ['verifier', 'verify', '驗證', '核對', '驗收'],
    'writer': ['writer', 'write', '寫作', '文件', '說明']
}

# 生命週期 Hook
HOOKS = {
    'stop': ['stop', 'hook', '生命週期', '分析', '阻斷'],
    'pretooluse': ['pretooluse', 'pre-tool', '工具', '阻斷', '攔截', '生命週期'],
    'posttooluse': ['posttooluse', 'post-tool', '工具', '生命週期', '分析'],
    'sessionstart': ['sessionstart', 'session-start', '開始', '啟動', '生命週期'],
    'sessionend': ['sessionend', 'session-end', '結束', '清理', '生命週期']
}

# 簡體中文獨有字集（繁體絕不包含，無 false positive）
SIMPLIFIED_ONLY_CHARS = set(
    "们时会发动国这关无学业个为样说两体队处权联组观认规设记应线统显场进导总图战开专单风仅较细类适资备属证义级"
    "书买乱争产众优伤伦伪余侠侦侧侨俭债倾储兑兰兴农冯冲冻净凄凉减凑凤凭凯击凿创剧劈劝办务劳势勋励匀区医华协单卖"
    "厂压厌厕厦厨双变叙叠叶号吗听呐唠唢唤哑哔哗哝哟园囵圣"
)

def check_simplified_chinese(content):
    """檢查內容中是否包含簡體字，返回包含的簡體字列表"""
    found = []
    for char in content:
        if char in SIMPLIFIED_ONLY_CHARS:
            found.append(char)
    return list(set(found))

def find_context_keywords(content, term, keywords, window=350):
    """在 term 出現位置的 window 長度上下文內，檢查是否包含至少一個關鍵字"""
    term_lower = term.lower()
    content_lower = content.lower()
    
    # 尋找所有匹配位置
    matches = [m.start() for m in re.finditer(re.escape(term_lower), content_lower)]
    if not matches:
        return False, "找不到名稱"
        
    for idx in matches:
        start = max(0, idx - window)
        end = min(len(content), idx + len(term) + window)
        snippet = content_lower[start:end]
        
        # 檢查關鍵字
        for kw in keywords:
            if kw.lower() in snippet:
                return True, snippet
                
    return False, f"有名稱但上下文未檢出核心作用關鍵字 (Checked keywords: {keywords})"

def validate_report(filepath):
    if not os.path.exists(filepath):
        print(f"Error: 報告檔案 {filepath} 不存在。")
        return False, ["File not found"]

    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()

    errors = []

    # 1. 檢查 41 個 skills
    print("--> 正在驗證 41 個 skills...")
    for skill, kws in SKILLS_KEYWORDS.items():
        # 檢查名稱
        if skill.lower() not in content.lower():
            errors.append(f"缺失 Skill 名稱: {skill}")
            continue
        # 檢查核心作用
        ok, msg = find_context_keywords(content, skill, kws)
        if not ok:
            errors.append(f"Skill '{skill}' 驗證失敗: {msg}")

    # 2. 檢查 19 個 agents
    print("--> 正在驗證 19 個 agents...")
    for agent, kws in AGENTS_KEYWORDS.items():
        if agent.lower() not in content.lower():
            errors.append(f"缺失 Agent 名稱: {agent}")
            continue
        ok, msg = find_context_keywords(content, agent, kws)
        if not ok:
            errors.append(f"Agent '{agent}' 驗證失敗: {msg}")

    # 3. 檢查生命週期 Hook
    print("--> 正在驗證生命週期 Hook...")
    for hook, kws in HOOKS.items():
        # 尋找 hook 名稱的變形（如 PreToolUse、Stop hook 等）
        hook_pattern = re.compile(re.escape(hook), re.IGNORECASE)
        # 特別處理 stop hook 這種複合詞
        if hook == 'stop':
            hook_pattern = re.compile(r'\bstop(\s+hook)?\b', re.IGNORECASE)
        elif hook == 'sessionstart':
            hook_pattern = re.compile(r'session\s*start', re.IGNORECASE)
        elif hook == 'sessionend':
            hook_pattern = re.compile(r'session\s*end', re.IGNORECASE)
            
        matches = list(hook_pattern.finditer(content))
        if not matches:
            errors.append(f"缺失生命週期 Hook 分析: {hook}")
            continue
            
        # 驗證上下文
        ok = False
        for m in matches:
            idx = m.start()
            start = max(0, idx - 300)
            end = min(len(content), idx + 300)
            snippet = content[start:end].lower()
            for kw in kws:
                if kw.lower() in snippet:
                    ok = True
                    break
            if ok:
                break
        if not ok:
            errors.append(f"生命週期 Hook '{hook}' 鄰近上下文缺乏分析內容 (Checked keywords: {kws})")

    # 4. 檢查 dual-review 和 ralplan 假綠漏洞分析與程式碼引用
    print("--> 正在驗證假綠漏洞分析與程式碼引用...")
    has_dual_review = "dual-review" in content.lower() or "dual review" in content.lower()
    has_ralplan = "ralplan" in content.lower()
    if not (has_dual_review and has_ralplan):
        errors.append("缺失對 dual-review 或 ralplan 的漏洞主體分析")
    
    # 尋找 whole-word APPROVE 關鍵字
    has_approve_parser = any(kw in content.lower() for kw in ['approve', 'parser', '假綠', 'negation', '否定'])
    if not has_approve_parser:
        errors.append("未分析 whole-word APPROVE parser 假綠漏洞相關機制")

    # 檢查代碼引用
    # 尋找像是 dual_review.py:85-124 或 L85 或 ralplan.py:256 這樣的引用
    code_ref_pattern = re.compile(
        r'(omg_cli/dual_review\.py|omg_cli/ralplan\.py|dual_review\.py|ralplan\.py)[^\n]{0,30}(L\d+|\d+)', 
        re.IGNORECASE
    )
    if not code_ref_pattern.search(content):
        errors.append("未提供 dual-review 和 ralplan 的程式碼檔案及行號引用 (例如 omg_cli/dual_review.py:85)")

    # 5. 檢查利用 Grok 原生 sessionId / --resume 實現 context continuity 的方案
    print("--> 正在驗證 Grok sessionId / --resume context continuity 方案...")
    has_session_id = "sessionid" in content.lower() or "session_id" in content.lower()
    has_resume = "resume" in content.lower()
    has_continuity = any(kw in content.lower() for kw in ['continuity', '持續性', '上下文持續'])
    if not (has_session_id and has_resume and has_continuity):
        errors.append("缺失利用 Grok 原生 sessionId / --resume 實現 context continuity 的方案分析")

    # 6. 檢查報告是否完全使用繁體中文
    print("--> 正在驗證繁體中文語系...")
    simplified_chars = check_simplified_chinese(content)
    if simplified_chars:
        errors.append(f"檢測到簡體中文獨有字: {', '.join(simplified_chars)}")

    if errors:
        print("\n[FAIL] 報告驗證未通過，發現以下缺失：")
        for err in errors:
            print(f" - {err}")
        return False, errors
    else:
        print("\n[PASS] 報告靜態分析驗證全部通過！")
        return True, []

if __name__ == '__main__':
    # Never hardcode absolute home paths; product pytest uses hermetic mocks only.
    target_path = None
    if len(sys.argv) > 1:
        target_path = sys.argv[1]
    elif os.environ.get("OMG_RESEARCH_REPORT_PATH"):
        target_path = os.environ["OMG_RESEARCH_REPORT_PATH"]
    if not target_path:
        print(
            "Usage: validate.py <report.md>\n"
            "   or: OMG_RESEARCH_REPORT_PATH=<report.md> validate.py",
            file=sys.stderr,
        )
        sys.exit(2)

    print(f"正在對報告進行靜態分析: {target_path}")
    success, errs = validate_report(target_path)
    if success:
        sys.exit(0)
    else:
        sys.exit(1)

