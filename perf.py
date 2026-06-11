import curses
import os
import re
import subprocess
import time

SQL_ID_PATTERN = re.compile(r"^[0-9a-zA-Z]{13}$")


def validate_sql_id(sql_id):
    """Return sql_id if valid (13 alphanumeric chars), else None."""
    if sql_id and SQL_ID_PATTERN.match(sql_id):
        return sql_id
    return None


def safe_addstr(win, y, x, text, attr=0):
    """Write text without raising curses.error on small terminals."""
    max_y, max_x = win.getmaxyx()
    if y < 0 or y >= max_y or x >= max_x:
        return
    clipped = str(text)[: max(0, max_x - x - 1)]
    if not clipped:
        return
    try:
        win.addstr(y, x, clipped, attr)
    except curses.error:
        pass


def wrap_text_to_lines(text, width):
    """Split text into display lines, wrapping long lines to width."""
    if not text or width < 1:
        return []
    lines = []
    for raw_line in text.replace("\r", "").split("\n"):
        line = raw_line.strip()
        if not line:
            lines.append("")
            continue
        while len(line) > width:
            lines.append(line[:width])
            line = line[width:]
        lines.append(line)
    return lines if lines else [""]


def draw_scroll_panel(stdscr, start_y, pane_height, pane_width, title, lines, scroll_top, focused, highlight_fn=None):
    """Draw a titled scrollable panel; returns number of rows used."""
    if pane_height < 3:
        return 0

    total = len(lines)
    visible = pane_height - 2
    if scroll_top < 0:
        scroll_top = 0
    if total > visible:
        max_scroll = total - visible
        if scroll_top > max_scroll:
            scroll_top = max_scroll

    if total == 0:
        scroll_hint = " (leer)"
    elif total <= visible:
        scroll_hint = f" ({total} Zeilen)"
    else:
        scroll_hint = f" ({scroll_top + 1}-{scroll_top + visible}/{total})"

    header_attr = curses.A_REVERSE | curses.A_BOLD if focused else curses.A_BOLD
    header = f" {title}{scroll_hint} "
    safe_addstr(stdscr, start_y, 0, header.ljust(pane_width)[: pane_width - 1], header_attr)
    safe_addstr(stdscr, start_y + 1, 0, "-" * (pane_width - 1))

    for row in range(visible):
        line_idx = scroll_top + row
        draw_y = start_y + 2 + row
        if line_idx >= total:
            break
        line = lines[line_idx]
        attr = curses.A_NORMAL
        if highlight_fn:
            attr = highlight_fn(line)
        safe_addstr(stdscr, draw_y, 0, line[: pane_width - 1], attr)

    return pane_height


def run_sqlplus(sql_command):
    """Führt ein SQL-Kommando über SQL*Plus als SYSDBA via OS-Call aus."""
    cmd = ["sqlplus", "-S", "/", "as", "sysdba"]
    formatted_sql = (
        "SET PAGESIZE 0;\n"
        "SET FEEDBACK OFF;\n"
        "SET LINESIZE 32767;\n"
        "SET TRIMSPOOL ON;\n"
        f"{sql_command}\n"
        "EXIT;\n"
    )
    try:
        current_env = os.environ.copy()
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            env=current_env,
        )
        stdout, stderr = proc.communicate(input=formatted_sql)
        if stderr and stderr.strip():
            return f"FEHLER: {stderr.strip()}"
        return stdout.strip()
    except Exception as e:
        return f"FEHLER: OS-Ausnahme {str(e)}"


def draw_dashboard(stdscr):
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_RED, -1)
    curses.init_pair(2, curses.COLOR_GREEN, -1)
    curses.init_pair(3, curses.COLOR_YELLOW, -1)

    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.keypad(True)

    mode = "OVERVIEW"
    sort_column = "elapsed_time"

    top_sql_list = []
    parsed_lines = []
    selected_sql_id = ""

    cursor_row = 0
    prev_cursor_row = -1
    refresh_frozen = False
    refresh_interval = 5
    countdown = 0
    cached_db_output = ""
    cached_waits_output = ""
    cached_xplan_output = ""
    cached_sql_text = ""
    sql_text_lines = []
    xplan_lines = []
    detail_focus = "SQL_TEXT"
    sql_scroll_top = 0
    xplan_scroll_top = 0
    cached_sess_output = ""
    cached_live_waits = ""
    cached_obj_stats = ""

    input_buffer = ""
    buffer_timeout = 0
    last_user_activity = time.time()
    last_frozen_fetch = 0
    last_obj_fetch = 0
    obj_refresh_interval = 2.0
    footer_force_refresh = True

    sub_mode = "SQL_SELECT"
    obj_cursor_row = 0
    obj_parsed_lines = []
    max_visible_objects = 4
    obj_scroll_top = 0

    while True:
        stdscr.erase()
        current_time = time.time()
        max_y, max_x = stdscr.getmaxyx()

        if max_y < 24 or max_x < 80:
            safe_addstr(stdscr, 0, 0, "Terminal zu klein. Mindestens 80x24 Zeichen erforderlich.")
            stdscr.refresh()
            time.sleep(0.5)
            continue

        line_width = min(140, max_x - 1)

        if refresh_frozen and (current_time - last_user_activity > 30.0):
            refresh_frozen = False
            countdown = 0

        if input_buffer and (current_time - buffer_timeout > 1.5):
            try:
                chosen_num = int(input_buffer)
                if 1 <= chosen_num <= len(parsed_lines):
                    cursor_row = chosen_num - 1
                    refresh_frozen = True
                    last_user_activity = current_time
            except ValueError:
                pass
            input_buffer = ""

        if cursor_row != prev_cursor_row:
            obj_cursor_row = 0
            obj_scroll_top = 0
            prev_cursor_row = cursor_row
            footer_force_refresh = True

        if countdown <= 0 and not refresh_frozen:
            if sort_column == "cpu_time":
                order_clause = "ORDER BY is_active_weight DESC, q.cpu_time_sec DESC"
            elif sort_column == "buffer_gets":
                order_clause = "ORDER BY is_active_weight DESC, q.buffer_gets_per_exec DESC"
            else:
                order_clause = "ORDER BY is_active_weight DESC, q.elapsed_time_sec DESC"

            SQL_TOP_20 = f"""
            SELECT
                q.sql_id || '|' || q.executions || '|' || q.cpu_time_sec || '|' || q.elapsed_time_sec || '|' || q.elap_per_exec || '|' || q.buffer_gets_per_exec || '|' || q.disk_reads_per_exec || '|' || NVL(p.fts, '---') || '|' || NVL(act.status, 'INA') || '|' || NVL(plans.plan_count, 1) || '|' || NVL(bl.has_bl, 'NEIN') || '|' || NVL(prof.has_pf, 'NEIN') || '|' || NVL(pq.dop, '---') || '|' || SUBSTR(REPLACE(q.sql_text, CHR(10), ' '), 1, 60)
            FROM (
                SELECT
                    sql_id, executions, elapsed_time, cpu_time, buffer_gets, disk_reads, sql_text, exact_matching_signature, sql_profile,
                    ROUND(cpu_time / 1000000, 2) as cpu_time_sec,
                    ROUND(elapsed_time / 1000000, 2) as elapsed_time_sec,
                    ROUND((elapsed_time / 1000000) / DECODE(executions, 0, 1, executions), 4) as elap_per_exec,
                    ROUND(buffer_gets / DECODE(executions, 0, 1, executions), 0) as buffer_gets_per_exec,
                    ROUND(disk_reads / DECODE(executions, 0, 1, executions), 0) as disk_reads_per_exec
                FROM v$sql
                WHERE executions > 0
                  AND sql_text NOT LIKE '%ORAMON_FILTER%'
                  AND sql_text NOT LIKE '%v$sql%'
                  AND sql_text NOT LIKE '%v$session%'
                  AND sql_text NOT LIKE '%v$active_session_history%'
                  AND sql_id IN (
                      SELECT DISTINCT sql_id FROM v$session
                      WHERE type != 'BACKGROUND'
                        AND username NOT IN ('SYSTEM', 'DBSNMP', 'SYS$BACKGROUND')
                        AND sql_id IS NOT NULL
                  )
            ) q
            LEFT JOIN (
                SELECT DISTINCT sql_id, 'FTS' AS fts FROM v$sql_plan WHERE operation = 'TABLE ACCESS' AND options = 'FULL'
            ) p ON q.sql_id = p.sql_id
            LEFT JOIN (
                SELECT DISTINCT sql_id, 'ACT' AS status FROM v$session WHERE status = 'ACTIVE' AND type != 'BACKGROUND' AND username NOT IN ('SYSTEM', 'DBSNMP')
            ) act ON q.sql_id = act.sql_id
            LEFT JOIN (
                SELECT sql_id, COUNT(DISTINCT plan_hash_value) AS plan_count FROM v$sql_plan GROUP BY sql_id
            ) plans ON q.sql_id = plans.sql_id
            LEFT JOIN (
                SELECT signature, 'JA' AS has_bl FROM dba_sql_plan_baselines WHERE accepted = 'YES' AND enabled = 'YES'
            ) bl ON q.exact_matching_signature = bl.signature
            LEFT JOIN (
                SELECT name, 'JA' AS has_pf FROM dba_sql_profiles WHERE status = 'ENABLED'
            ) prof ON q.sql_profile = prof.name
            LEFT JOIN (
                SELECT s.sql_id, 'x' || COUNT(distinct px.sid) as dop
                FROM v$px_session px
                JOIN v$session s ON px.qcsid = s.sid
                WHERE s.sql_id IS NOT NULL
                GROUP BY s.sql_id
            ) pq ON q.sql_id = pq.sql_id
            CROSS JOIN (
                SELECT q.sql_id, DECODE(NVL(act.status, 'INA'), 'ACT', 2, 1) AS is_active_weight
                FROM v$sql q
                LEFT JOIN (SELECT DISTINCT sql_id, 'ACT' AS status FROM v$session WHERE status = 'ACTIVE' AND type != 'BACKGROUND' AND username NOT IN ('SYSTEM')) act ON q.sql_id = act.sql_id
            ) wht WHERE q.sql_id = wht.sql_id
            {order_clause}
            FETCH FIRST 20 ROWS ONLY;
            """
            cached_db_output = run_sqlplus(SQL_TOP_20)

            parsed_lines = []
            top_sql_list = []
            if cached_db_output and "FEHLER" not in cached_db_output:
                for line in cached_db_output.split("\n"):
                    cleaned = line.strip()
                    if "|" in cleaned:
                        parts = cleaned.split("|")
                        if len(parts) >= 14:
                            parsed_lines.append([p.strip() for p in parts])
                            top_sql_list.append(parts[0].strip())

            if cursor_row >= len(parsed_lines) and parsed_lines:
                cursor_row = len(parsed_lines) - 1

            countdown = refresh_interval

        if mode == "OVERVIEW" and top_sql_list and cursor_row < len(top_sql_list):
            obj_scroll_due = (
                sub_mode == "OBJ_SCROLL"
                and current_time - last_obj_fetch >= obj_refresh_interval
            )
            footer_due = (
                footer_force_refresh
                or not refresh_frozen
                or (current_time - last_frozen_fetch > 30.0)
                or obj_scroll_due
            )

            if footer_due:
                current_cursor_sql_id = validate_sql_id(top_sql_list[cursor_row])
                footer_force_refresh = False

                if current_cursor_sql_id:
                    SQL_SESS_DETAIL = f"""
                    SELECT s.sid || '|' || s.serial# || '|' || NVL(s.username, 'BACKGROUND') || '|' || SUBSTR(s.program,1,30) || '|' || NVL(SUBSTR(s.module,1,25), '---') || '|' || SUBSTR(s.machine,1,25) || '|' || NVL(b.blocker_status, 'NO_BLOCK')
                    FROM v$session s
                    LEFT JOIN (
                        SELECT DISTINCT blocking_session, 'BLOCKER' as blocker_status FROM v$session WHERE blocking_session IS NOT NULL
                        ) b ON s.sid = b.blocking_session
                    WHERE (s.sql_id = '{current_cursor_sql_id}' OR s.prev_sql_id = '{current_cursor_sql_id}')
                      AND ROWNUM = 1;
                    """
                    cached_sess_output = run_sqlplus(SQL_SESS_DETAIL)

                    SQL_LIVE_EVENT = f"""
                    SELECT DECODE(state, 'WAITING', event, 'ON CPU / PROCESSING') FROM v$session WHERE (sql_id = '{current_cursor_sql_id}' OR prev_sql_id = '{current_cursor_sql_id}') AND ROWNUM = 1;
                    """
                    cached_live_waits = run_sqlplus(SQL_LIVE_EVENT)

                    SQL_OBJ_STATS = f"""
                    SELECT object_owner || '|' || object_name || '|' || TO_CHAR(last_analyzed, 'DD.MM.YYYY HH24:MI') || '|' || object_type
                    FROM (
                        SELECT p.object_owner, p.object_name, t.last_analyzed, 'TABLE' as object_type
                        FROM v$sql_plan p
                        JOIN dba_tables t ON p.object_owner = t.owner AND p.object_name = t.table_name
                        WHERE p.sql_id = '{current_cursor_sql_id}' AND p.object_name IS NOT NULL
                        UNION ALL
                        SELECT p.object_owner, p.object_name, i.last_analyzed, 'INDEX' as object_type
                        FROM v$sql_plan p
                        JOIN dba_indexes i ON p.object_owner = i.owner AND p.object_name = i.index_name
                        WHERE p.sql_id = '{current_cursor_sql_id}' AND p.object_name IS NOT NULL
                    ) WHERE ROWNUM <= 30;
                    """
                    cached_obj_stats = run_sqlplus(SQL_OBJ_STATS)
                else:
                    cached_sess_output = ""
                    cached_live_waits = ""
                    cached_obj_stats = ""

                obj_parsed_lines = []
                if cached_obj_stats and "FEHLER" not in cached_obj_stats and cached_obj_stats.strip():
                    for o_line in cached_obj_stats.split("\n"):
                        if "|" in o_line:
                            o_parts = o_line.split("|")
                            if len(o_parts) >= 4:
                                obj_parsed_lines.append([o.strip() for o in o_parts])

                if obj_cursor_row >= len(obj_parsed_lines) and obj_parsed_lines:
                    obj_cursor_row = len(obj_parsed_lines) - 1
                    obj_scroll_top = max(0, obj_cursor_row - max_visible_objects + 1)

                if refresh_frozen and sub_mode != "OBJ_SCROLL":
                    last_frozen_fetch = current_time
                if sub_mode == "OBJ_SCROLL":
                    last_obj_fetch = current_time

        if mode == "SQL_DETAIL" and selected_sql_id and countdown <= 0 and not refresh_frozen:
            safe_sql_id = validate_sql_id(selected_sql_id)
            if safe_sql_id:
                SQL_WAITS = f"SELECT event || '|' || COUNT(*) FROM v$active_session_history WHERE sql_id = '{safe_sql_id}' AND event IS NOT NULL GROUP BY event ORDER BY COUNT(*) DESC FETCH FIRST 5 ROWS ONLY;"
                cached_waits_output = run_sqlplus(SQL_WAITS)
                SQL_TEXT = f"SELECT sql_fulltext FROM v$sql WHERE sql_id = '{safe_sql_id}' AND ROWNUM = 1;"
                cached_sql_text = run_sqlplus(SQL_TEXT)
                SQL_XPLAN = f"SELECT plan_table_output FROM TABLE(DBMS_XPLAN.DISPLAY_CURSOR('{safe_sql_id}', NULL, 'TYPICAL'));"
                cached_xplan_output = run_sqlplus(SQL_XPLAN)
            else:
                cached_waits_output = "FEHLER: Ungueltige SQL_ID"
                cached_sql_text = ""
                cached_xplan_output = ""

            pane_width = max(40, max_x - 1)
            if cached_sql_text and "FEHLER" not in cached_sql_text:
                sql_text_lines = wrap_text_to_lines(cached_sql_text.strip(), pane_width)
            else:
                sql_text_lines = ["SQL-Text konnte nicht aus v$sql gelesen werden."]

            if cached_xplan_output and "FEHLER" not in cached_xplan_output and cached_xplan_output.strip():
                xplan_lines = [ln.rstrip() for ln in cached_xplan_output.split("\n") if ln.strip() or ln == ""]
                if not xplan_lines:
                    xplan_lines = ["Ausfuehrungsplan ist leer."]
            else:
                xplan_lines = ["Ausfuehrungsplan konnte nicht aus dem Cursor-Cache gelesen werden."]

            sql_visible = max(1, (max_y - 14) // 2 - 2)
            xplan_visible = max(1, max_y - 14 - sql_visible - 2)
            sql_max_scroll = max(0, len(sql_text_lines) - sql_visible)
            xplan_max_scroll = max(0, len(xplan_lines) - xplan_visible)
            sql_scroll_top = min(sql_scroll_top, sql_max_scroll)
            xplan_scroll_top = min(xplan_scroll_top, xplan_max_scroll)

            countdown = refresh_interval

        sort_label = "ELAPSED TIME" if sort_column == "elapsed_time" else ("CPU-ZEIT" if sort_column == "cpu_time" else "BUFFER GETS")
        mode_label = "OBJ-SCROLL" if sub_mode == "OBJ_SCROLL" else "SQL-SELECT"

        if refresh_frozen:
            time_left_unfreeze = int(30 - (current_time - last_user_activity))
            refresh_status = f"PAUSIERT (Resume in {time_left_unfreeze}s)"
        else:
            refresh_status = f"Aktiv ({int(countdown)}s)"

        typing_status = f"Tippe Nr: {input_buffer}" if input_buffer else ""

        if mode == "SQL_DETAIL":
            detail_label = "SQL-TEXT" if detail_focus == "SQL_TEXT" else "XPLAN"
            header_line = " ORAMON v6.0 DEEP DIVE | [s]/[<-] Zurueck  [o] Fokus SQL/Plan  [Pfeile] Scroll  [r] Refresh  [q] Exit"
            status_line = f" Zeit: {time.strftime('%H:%M:%S')}  |  SQL_ID: {selected_sql_id:<13}  |  {refresh_status:<22} Fokus: {detail_label:<8}"
        else:
            header_line = " ORAMON v6.0 | [o] Modus  [Pfeile] Nav  [ENTER] XPlan  [e/c/b] Sort  [r] Refresh  [q] Exit"
            status_line = f" Zeit: {time.strftime('%H:%M:%S')}  |  Sortierung: {sort_label:<12}  |  {refresh_status:<22} Fokus: {mode_label:<10} {typing_status}"

        safe_addstr(stdscr, 0, 0, header_line, curses.A_REVERSE)
        safe_addstr(stdscr, 1, 0, status_line)
        safe_addstr(stdscr, 2, 0, "=" * line_width)

        if mode == "OVERVIEW":
            elap_attr = curses.A_UNDERLINE if sort_column == "elapsed_time" else curses.A_NORMAL
            cpu_attr = curses.A_UNDERLINE if sort_column == "cpu_time" else curses.A_NORMAL

            safe_addstr(stdscr, 4, 0, "TOP 20 APPLICATION SQL MONITOR (Druecke [o] um in das untere Objekt-Fenster zu springen):", curses.A_BOLD)
            safe_addstr(stdscr, 5, 0, f"{'NR':<3} | {'SQL_ID':<15} | {'EXECUTIONS':<10} | ", curses.A_BOLD)
            safe_addstr(stdscr, 5, 36, f"{'CPU (Sek)':<10}", curses.A_BOLD | cpu_attr)
            safe_addstr(stdscr, 5, 46, " | ", curses.A_BOLD)
            safe_addstr(stdscr, 5, 49, f"{'ELAPSED (s)':<11}", curses.A_BOLD | elap_attr)
            safe_addstr(
                stdscr,
                5,
                60,
                f" | {'STAT':<4} | {'SCAN':<4} | {'PLANS':<5} | {'BL':<4} | {'PF':<4} | {'PQ':<4} | {'SQL Snippet':<20}",
                curses.A_BOLD,
            )
            safe_addstr(stdscr, 6, 0, "-" * line_width)

            if not parsed_lines:
                safe_addstr(stdscr, 8, 2, "Warte auf Applikationslast...", curses.A_DIM)
            else:
                visible_rows = min(len(parsed_lines), max(0, max_y - 8))
                for idx in range(visible_rows):
                    parts = parsed_lines[idx]
                    if len(parts) < 14:
                        continue

                    sql_id, execs, cpu, elap, ela_exe, gets, reads, scan, status, plan_count, baseline_flag, profile_flag, pq_flag = parts[:13]
                    text = "|".join(parts[13:])
                    row_num = idx + 1

                    if idx == cursor_row:
                        line_attr = curses.A_REVERSE if sub_mode == "SQL_SELECT" else curses.A_BOLD | curses.A_UNDERLINE
                    else:
                        try:
                            is_slow = float(ela_exe) > 10.0
                        except ValueError:
                            is_slow = False
                        line_attr = curses.color_pair(3) | curses.A_BOLD if is_slow else curses.A_NORMAL

                    prefix_str = f"{row_num:<3} | {sql_id:<15} | {execs:<10} | {cpu:<10} | {elap:<11} | "
                    safe_addstr(stdscr, 7 + idx, 0, prefix_str, line_attr)

                    stat_pos = len(prefix_str)
                    if idx == cursor_row and sub_mode == "SQL_SELECT":
                        safe_addstr(stdscr, 7 + idx, stat_pos, f"{status:<4}", curses.A_REVERSE | curses.A_BOLD)
                    else:
                        safe_addstr(stdscr, 7 + idx, stat_pos, f"{status:<4}", curses.color_pair(2 if status == "ACT" else 1) | curses.A_BOLD)

                    scan_pos = stat_pos + 7
                    safe_addstr(stdscr, 7 + idx, scan_pos - 3, " | ", line_attr)
                    if scan == "FTS":
                        if idx == cursor_row and sub_mode == "SQL_SELECT":
                            safe_addstr(stdscr, 7 + idx, scan_pos, f"{scan:<4}", curses.A_REVERSE | curses.A_BOLD)
                        else:
                            safe_addstr(stdscr, 7 + idx, scan_pos, f"{scan:<4}", curses.color_pair(1) | curses.A_BOLD)
                    else:
                        safe_addstr(stdscr, 7 + idx, scan_pos, f"{scan:<4}", line_attr)

                    plan_pos = scan_pos + 7
                    safe_addstr(stdscr, 7 + idx, plan_pos - 3, " | ", line_attr)
                    try:
                        p_cnt = int(plan_count)
                    except ValueError:
                        p_cnt = 1
                    if idx == cursor_row and sub_mode == "SQL_SELECT":
                        safe_addstr(stdscr, 7 + idx, plan_pos, f"{plan_count:<5}", curses.A_REVERSE | curses.A_BOLD)
                    else:
                        if p_cnt > 1:
                            safe_addstr(stdscr, 7 + idx, plan_pos, f"{plan_count:<5}", curses.color_pair(1) | curses.A_BOLD)
                        else:
                            safe_addstr(stdscr, 7 + idx, plan_pos, f"{plan_count:<5}", line_attr)

                    bl_pos = plan_pos + 8
                    safe_addstr(stdscr, 7 + idx, bl_pos - 3, " | ", line_attr)
                    if idx == cursor_row and sub_mode == "SQL_SELECT":
                        safe_addstr(stdscr, 7 + idx, bl_pos, f"{baseline_flag:<4}", curses.A_REVERSE | curses.A_BOLD)
                    else:
                        if baseline_flag == "JA":
                            safe_addstr(stdscr, 7 + idx, bl_pos, f"{baseline_flag:<4}", curses.color_pair(2) | curses.A_BOLD)
                        else:
                            safe_addstr(stdscr, 7 + idx, bl_pos, f"{baseline_flag:<4}", line_attr)

                    pf_pos = bl_pos + 7
                    safe_addstr(stdscr, 7 + idx, pf_pos - 3, " | ", line_attr)
                    if idx == cursor_row and sub_mode == "SQL_SELECT":
                        safe_addstr(stdscr, 7 + idx, pf_pos, f"{profile_flag:<4}", curses.A_REVERSE | curses.A_BOLD)
                    else:
                        if profile_flag == "JA":
                            safe_addstr(stdscr, 7 + idx, pf_pos, f"{profile_flag:<4}", curses.color_pair(2) | curses.A_BOLD)
                        else:
                            safe_addstr(stdscr, 7 + idx, pf_pos, f"{profile_flag:<4}", line_attr)

                    pq_pos = pf_pos + 7
                    safe_addstr(stdscr, 7 + idx, pq_pos - 3, " | ", line_attr)
                    if idx == cursor_row and sub_mode == "SQL_SELECT":
                        safe_addstr(stdscr, 7 + idx, pq_pos, f"{pq_flag:<4}", curses.A_REVERSE | curses.A_BOLD)
                    else:
                        if pq_flag != "---":
                            safe_addstr(stdscr, 7 + idx, pq_pos, f"{pq_flag:<4}", curses.color_pair(2) | curses.A_BOLD)
                        else:
                            safe_addstr(stdscr, 7 + idx, pq_pos, f"{pq_flag:<4}", line_attr)

                    safe_addstr(stdscr, 7 + idx, pq_pos + 4, f" | {text[:20]:<20}", line_attr)

            if parsed_lines and cursor_row < len(parsed_lines) and max_y > 28:
                p_item = parsed_lines[cursor_row]
                ela_exe, gets, reads = p_item[4], p_item[5], p_item[6]
                full_text = "|".join(p_item[13:])

                safe_addstr(stdscr, 28, 0, f"Effizienz:  Durchschnitt: {ela_exe:<6}s | Logische Reads: {gets:<10} | Physische Reads: {reads}")

                if cached_sess_output and "FEHLER" not in cached_sess_output and "|" in cached_sess_output:
                    sid, serial, username, program, module, machine, blocker = [s.strip() for s in cached_sess_output.split("|")]
                    safe_addstr(
                        stdscr,
                        29,
                        0,
                        f"Session:    SID/Serial: {sid},{serial:<6} User: {username:<10} Module: {module[:15]:<15} Machine: {machine[:15]:<15} Prog: {program[:15]}",
                    )
                    safe_addstr(stdscr, 30, 0, "BLOCKER:    ")
                    if blocker == "BLOCKER":
                        safe_addstr(stdscr, 30, 11, "!!! DIESE SESSION BLOCKIERT AKTIV ANDERE SITZUNGEN !!!", curses.color_pair(1) | curses.A_BOLD)
                    else:
                        safe_addstr(stdscr, 30, 11, "KEIN BLOCKER - Session blockiert keine anderen Prozesse.", curses.color_pair(2) | curses.A_BOLD)
                    safe_addstr(stdscr, 31, 0, "LIVE WAITS: ")
                    if cached_live_waits and "FEHLER" not in cached_live_waits and cached_live_waits.strip():
                        safe_addstr(stdscr, 31, 11, cached_live_waits.strip(), curses.A_BOLD)
                    else:
                        safe_addstr(stdscr, 31, 11, "ON CPU / PROCESSING", curses.color_pair(2) | curses.A_BOLD)
                    safe_addstr(stdscr, 32, 0, f"SQL-Text:  {full_text[: max(0, max_x - 12)]}")
                else:
                    safe_addstr(stdscr, 29, 0, "Session:    Keine aktive Zuordnung in v$session (Mikropause oder Statement beendet).", curses.A_DIM)
                    safe_addstr(stdscr, 30, 0, f"SQL-Text:  {full_text[: max(0, max_x - 12)]}")

                if max_y > 34:
                    scroll_indicator = (
                        f" (Scroll aktiv: Zeile {obj_cursor_row + 1}/{len(obj_parsed_lines)})"
                        if sub_mode == "OBJ_SCROLL"
                        else " [o] Druecken zum Scrollen"
                    )
                    safe_addstr(
                        stdscr,
                        34,
                        0,
                        f"{'OBJECT OWNER':<15} | {'OBJECT NAME':<30} | {'LAST ANALYZED':<17} | {'TYPE':<6} |{scroll_indicator:<25}",
                        curses.A_BOLD | curses.A_REVERSE,
                    )
                    safe_addstr(stdscr, 35, 0, "-" * min(95, line_width))

                    if not obj_parsed_lines:
                        safe_addstr(stdscr, 36, 2, "Keine Tabellen/Index-Objektzuordnungen im aktuellen Cursor-Cache gefunden.", curses.A_DIM)
                    else:
                        if obj_cursor_row < obj_scroll_top:
                            obj_scroll_top = obj_cursor_row
                        elif obj_cursor_row >= obj_scroll_top + max_visible_objects:
                            obj_scroll_top = obj_cursor_row - max_visible_objects + 1

                        for v_idx in range(max_visible_objects):
                            curr_obj_idx = obj_scroll_top + v_idx
                            if curr_obj_idx >= len(obj_parsed_lines) or 36 + v_idx >= max_y:
                                break

                            o_owner, o_name, o_date, o_type = obj_parsed_lines[curr_obj_idx]
                            obj_attr = curses.A_REVERSE | curses.A_BOLD if sub_mode == "OBJ_SCROLL" and curr_obj_idx == obj_cursor_row else curses.A_NORMAL
                            safe_addstr(stdscr, 36 + v_idx, 0, f"{o_owner:<15} | {o_name:<30} | {o_date:<17} | {o_type:<6}", obj_attr)

        elif mode == "SQL_DETAIL":
            pane_width = max(40, max_x - 1)
            safe_addstr(stdscr, 4, 0, "Top Wait Events (ASH):", curses.A_BOLD)
            w_idx = 0
            for line in cached_waits_output.split("\n")[:3]:
                cleaned_w = line.strip()
                if "|" in cleaned_w:
                    event, samples = cleaned_w.split("|", 1)
                    safe_addstr(
                        stdscr,
                        5 + w_idx,
                        2,
                        f"- {event.strip():<40} (Samples: {samples.strip()})",
                        curses.A_NORMAL,
                    )
                    w_idx += 1
            if w_idx == 0:
                safe_addstr(stdscr, 5, 2, "Keine ASH Wait Events fuer diese SQL_ID.", curses.A_DIM)

            ash_rows = 6
            available = max(10, max_y - ash_rows - 1)
            sql_pane_height = max(5, available // 2)
            xplan_pane_height = max(5, available - sql_pane_height)

            def xplan_highlight(line):
                if "TABLE ACCESS FULL" in line:
                    return curses.color_pair(1) | curses.A_BOLD
                return curses.A_NORMAL

            sql_start = ash_rows
            draw_scroll_panel(
                stdscr,
                sql_start,
                sql_pane_height,
                pane_width,
                "SQL TEXT",
                sql_text_lines,
                sql_scroll_top,
                detail_focus == "SQL_TEXT",
            )

            xplan_start = sql_start + sql_pane_height
            if xplan_start < max_y - 3:
                draw_scroll_panel(
                    stdscr,
                    xplan_start,
                    xplan_pane_height,
                    pane_width,
                    "EXECUTION PLAN (DBMS_XPLAN)",
                    xplan_lines,
                    xplan_scroll_top,
                    detail_focus == "XPLAN",
                    highlight_fn=xplan_highlight,
                )

        stdscr.refresh()

        try:
            key = stdscr.getch()
            if key == ord("q"):
                break

            elif key == ord("o"):
                if mode == "OVERVIEW" and obj_parsed_lines:
                    sub_mode = "OBJ_SCROLL" if sub_mode == "SQL_SELECT" else "SQL_SELECT"
                    last_user_activity = current_time
                    if sub_mode == "OBJ_SCROLL":
                        last_obj_fetch = 0
                        footer_force_refresh = True
                elif mode == "SQL_DETAIL":
                    detail_focus = "XPLAN" if detail_focus == "SQL_TEXT" else "SQL_TEXT"
                    last_user_activity = current_time
                    refresh_frozen = True

            elif key == curses.KEY_DOWN:
                last_user_activity = current_time
                refresh_frozen = True
                if mode == "SQL_DETAIL":
                    if detail_focus == "SQL_TEXT":
                        sql_scroll_top += 1
                    else:
                        xplan_scroll_top += 1
                elif sub_mode == "SQL_SELECT":
                    if parsed_lines:
                        cursor_row = (cursor_row + 1) % len(parsed_lines)
                elif obj_parsed_lines:
                    obj_cursor_row = (obj_cursor_row + 1) % len(obj_parsed_lines)

            elif key == curses.KEY_UP:
                last_user_activity = current_time
                refresh_frozen = True
                if mode == "SQL_DETAIL":
                    if detail_focus == "SQL_TEXT":
                        sql_scroll_top = max(0, sql_scroll_top - 1)
                    else:
                        xplan_scroll_top = max(0, xplan_scroll_top - 1)
                elif sub_mode == "SQL_SELECT":
                    if parsed_lines:
                        cursor_row = (cursor_row - 1) % len(parsed_lines)
                elif obj_parsed_lines:
                    obj_cursor_row = (obj_cursor_row - 1) % len(obj_parsed_lines)

            elif key in [curses.KEY_ENTER, 10, 13]:
                if mode == "OVERVIEW" and top_sql_list and sub_mode == "SQL_SELECT":
                    candidate = validate_sql_id(top_sql_list[cursor_row])
                    if candidate:
                        selected_sql_id = candidate
                        mode = "SQL_DETAIL"
                        detail_focus = "SQL_TEXT"
                        sql_scroll_top = 0
                        xplan_scroll_top = 0
                        countdown = 0
                        refresh_frozen = False

            elif key in [ord("s"), curses.KEY_LEFT]:
                if mode == "SQL_DETAIL":
                    mode = "OVERVIEW"
                    countdown = 0
                    refresh_frozen = False
                elif sub_mode == "OBJ_SCROLL":
                    sub_mode = "SQL_SELECT"

            elif key == ord("r"):
                refresh_frozen = False
                countdown = 0
                input_buffer = ""
                footer_force_refresh = True
                if mode == "SQL_DETAIL":
                    sql_scroll_top = 0
                    xplan_scroll_top = 0
            elif key == ord("e") and sub_mode == "SQL_SELECT":
                sort_column = "elapsed_time"
                countdown = 0
            elif key == ord("c") and sub_mode == "SQL_SELECT":
                sort_column = "cpu_time"
                countdown = 0
            elif key == ord("b") and sub_mode == "SQL_SELECT":
                sort_column = "buffer_gets"
                countdown = 0
            elif mode == "OVERVIEW" and ord("0") <= key <= ord("9") and sub_mode == "SQL_SELECT":
                input_buffer += chr(key)
                buffer_timeout = current_time
                last_user_activity = current_time
        except IOError:
            pass

        time.sleep(0.1)
        if not refresh_frozen:
            countdown -= 0.1


if __name__ == "__main__":
    curses.wrapper(draw_dashboard)
