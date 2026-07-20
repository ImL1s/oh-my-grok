import os
import tempfile
import pytest
from tests.report_validator.validate import validate_report

def generate_valid_mock_markdown():
    # 建立包含 41 個 skills 名稱與核心作用關鍵字的內容
    skills_section = "\n".join([
        f"- **{skill}**: 這是一個用於執行{kws[0]}和進行{kws[-1]}的 OMC 技能。"
        for skill, kws in {
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
        }.items()
    ])

    # 建立包含 19 個 agents 名稱與核心作用關鍵字的內容
    agents_section = "\n".join([
        f"- **{agent}**: 此代理主要負責{kws[0]}與{kws[-1]}的協調與運作。"
        for agent, kws in {
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
        }.items()
    ])

    # 生命週期 Hook 的描述
    hooks_section = """
    ## 生命週期 Hook 分析
    - **Stop hook**: 關於 Stop hook 的生命週期分析與阻斷機制。
    - **PreToolUse**: 用於在工具調用前攔截與阻斷 PreToolUse。
    - **PostToolUse**: 提供工具調用後分析的 PostToolUse 機制。
    - **SessionStart**: 關於會話開始與 Session Start 啟動的分析。
    - **SessionEnd**: 關於會話結束與 Session End 清理的分析。
    """

    # 假綠漏洞分析與程式碼引用
    vuln_section = """
    ## 假綠漏洞與代碼引用
    在 dual-review 與 ralplan 中，whole-word APPROVE parser 存在嚴重的「假綠」漏洞。
    當 verifier 寫入 Negation (否定句) 如 "Do not APPROVE" 時，parser 會錯誤地判定為通過。
    此程式碼引用參見：
    - `omg_cli/dual_review.py:85` 行中的 prose parser 實作。
    - `omg_cli/ralplan.py:256` 中關於 consensus 的檢查。
    """

    # sessionId 與 continuity 方案
    continuity_section = """
    ## 跨 Mode 上下文持續方案
    我們可以使用 Grok 原生提供的 sessionId 機制，並在啟動時帶入 --resume 參數，
    從而實現 context continuity 的上下文持續性方案。
    """

    # 組合全部內容 (必須完全為繁體中文)
    markdown_content = f"""
# OMC 與 OMX 機制研究報告

## Skills 列表
{skills_section}

## Agents 列表
{agents_section}

{hooks_section}

{vuln_section}

{continuity_section}
    """
    return markdown_content

def test_mock_report_validation_pass():
    """驗證當 Markdown 內容完全合法時，驗證器是否能通過"""
    content = generate_valid_mock_markdown()
    
    # 寫入暫存檔案進行驗證
    with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False, encoding='utf-8') as temp:
        temp.write(content)
        temp_path = temp.name

    try:
        success, errors = validate_report(temp_path)
        assert success, f"應該要通過驗證，但失敗了。錯誤: {errors}"
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

def test_mock_report_validation_fail_simplified_chinese():
    """驗證當含有簡體中文獨有字時，驗證是否失敗"""
    content = generate_valid_mock_markdown()
    # 故意混入簡體字 "们"
    content += "\n這是一段包含 们 的簡體中文文字。"
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False, encoding='utf-8') as temp:
        temp.write(content)
        temp_path = temp.name

    try:
        success, errors = validate_report(temp_path)
        assert not success
        assert any("簡體中文獨有字" in err for err in errors)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)
