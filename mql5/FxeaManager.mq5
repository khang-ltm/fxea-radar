//+------------------------------------------------------------------+
//|  FxeaManager.mq5 - pause and resume EAs for the FX EA Radar agent |
//|                                                                   |
//|  Attach ONCE to a spare chart (any symbol, any timeframe). It     |
//|  never trades.                                                    |
//|                                                                   |
//|  MT5 has no "pause an EA" call, so pause is done honestly:        |
//|    pause  = ChartSaveTemplate (keeps the EA AND its inputs)       |
//|             then ChartClose, which stops the EA                   |
//|    run    = ChartOpen, then ChartApplyTemplate, which brings the  |
//|             EA back with the same settings                        |
//|  The saved template is what makes this reversible; without it a   |
//|  closed chart would lose the EA's parameters.                     |
//|                                                                   |
//|  Talks to the Python agent through files in MQL5\Files, so there  |
//|  is no WebRequest permission to grant and no token inside MQL5:   |
//|      fxea_cmd.txt     <- one command, written by the agent        |
//|      fxea_status.json -> charts + paused list, written here       |
//|      fxea_result.txt  -> outcome of the last command              |
//|      fxea_paused.txt  -> what is paused, so it survives restarts  |
//|                                                                   |
//|  A chart reports only the EA file name, never its settings, so    |
//|  the status file also carries the inputs - magic, lots, risk -    |
//|  read out of a throwaway template saved from that chart.          |
//|                                                                   |
//|  Timer driven, so it still answers on a quiet symbol or when the  |
//|  market is closed.                                                |
//+------------------------------------------------------------------+
#property copyright "FX EA Radar"
#property version   "1.14"
#property strict

input int  TimerSeconds    = 1;      // how often to poll for a command
input int  StatusEverySecs = 5;      // how often to rewrite the status file
input bool AllowControl    = true;   // master switch for pause / run
input bool AllowEditInputs = true;   // let the page change EA settings
input bool VerboseLog      = true;   // print actions to the Experts log
input bool ReportInputs    = true;   // report each EA settings (magic, lots, ...)

#define CMD_FILE     "fxea_cmd.txt"
#define RESULT_FILE  "fxea_result.txt"
#define STATUS_FILE  "fxea_status.json"
#define PAUSED_FILE  "fxea_paused.txt"
#define INPUTS_FILE  "fxea_inputs.txt"
#define EDIT_NAME    "fxea_edit"

datetime g_last_status = 0;
long     g_self_chart  = 0;

//+------------------------------------------------------------------+
int OnInit()
  {
   g_self_chart = ChartID();
   EventSetTimer(MathMax(1, TimerSeconds));
   if(VerboseLog)
      PrintFormat("FxeaManager %s on chart %I64d (%s). Control allowed: %s",
                  "1.14", g_self_chart, _Symbol, AllowControl ? "yes" : "no");
   WriteStatus();
   return(INIT_SUCCEEDED);
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
   if(VerboseLog)
      PrintFormat("FxeaManager stopped (reason %d)", reason);
  }

void OnTimer()
  {
   ProcessCommand();
   if(TimeCurrent() - g_last_status >= StatusEverySecs)
     {
      WriteStatus();
      g_last_status = TimeCurrent();
     }
  }

//+------------------------------------------------------------------+
string JsonEscape(const string raw)
  {
   string out = raw;
   StringReplace(out, "\\", "\\\\");
   StringReplace(out, "\"", "\\\"");
   StringReplace(out, "\n", " ");
   StringReplace(out, "\r", " ");
   StringReplace(out, "\t", " ");
   return(out);
  }

//+------------------------------------------------------------------+
//| Paused register: one record per line                              |
//|   key|symbol|period|expert|template                               |
//| Kept on disk so a terminal restart does not lose what was paused. |
//+------------------------------------------------------------------+
int LoadPaused(string &out[])
  {
   ArrayResize(out, 0);
   if(!FileIsExist(PAUSED_FILE))
      return(0);
   int fh = FileOpen(PAUSED_FILE, FILE_READ | FILE_TXT | FILE_ANSI);
   if(fh == INVALID_HANDLE)
      return(0);
   while(!FileIsEnding(fh))
     {
      string line = FileReadString(fh);
      StringTrimLeft(line);
      StringTrimRight(line);
      if(StringLen(line) > 0)
        {
         int n = ArraySize(out);
         ArrayResize(out, n + 1);
         out[n] = line;
        }
     }
   FileClose(fh);
   return(ArraySize(out));
  }

void SavePaused(const string &rows[])
  {
   int fh = FileOpen(PAUSED_FILE, FILE_WRITE | FILE_TXT | FILE_ANSI);
   if(fh == INVALID_HANDLE)
      return;
   for(int i = 0; i < ArraySize(rows); i++)
      FileWriteString(fh, rows[i] + "\n");
   FileClose(fh);
  }

string Part(const string row, const int index)
  {
   string bits[];
   int n = StringSplit(row, (ushort)'|', bits);
   return(index < n ? bits[index] : "");
  }


//+------------------------------------------------------------------+
//| The magic number a chart trades under.                            |
//|                                                                   |
//| MT5 exposes the EA file name on a chart and the magic number on a |
//| trade, and nothing that joins the two. The inputs do hold it, and |
//| a saved template is the only way to read another EA inputs, so    |
//| this dumps a throwaway template into MQL5\Files, greps the first  |
//| input under <expert> whose name mentions "magic", and deletes it. |
//| Cached per chart+expert: templates are disk writes, not free.     |
//+------------------------------------------------------------------+
#define PROBE_NAME "fxea_probe"

long   g_pm_chart[];
string g_pm_expert[];
long   g_pm_magic[];
string g_pm_inputs[];

bool ReadInputsFromTemplate(const long id, long &magic, string &json)
  {
   magic = 0;
   json  = "";

   string file = PROBE_NAME + ".tpl";
   if(FileIsExist(file))
      FileDelete(file);
   if(!ChartSaveTemplate(id, "\\Files\\" + PROBE_NAME))
      return(false);

   // chart commands are queued, so the file appears a moment later
   for(int wait = 0; wait < 20 && !FileIsExist(file); wait++)
      Sleep(50);
   if(!FileIsExist(file))
      return(false);

   int fh = FileOpen(file, FILE_READ | FILE_TXT | FILE_ANSI);
   if(fh == INVALID_HANDLE)
     {
      FileDelete(file);
      return(false);
     }

   bool in_expert = false;                 // indicators carry inputs too
   bool in_inputs = false;
   int  count     = 0;

   while(!FileIsEnding(fh))
     {
      string line = FileReadString(fh);
      StringTrimLeft(line);
      StringTrimRight(line);
      string low = line;
      StringToLower(low);

      if(low == "<expert>")
         in_expert = true;
      else
         if(low == "</expert>")
            break;                         // one EA per chart: nothing after it
         else
            if(in_expert && low == "<inputs>")
               in_inputs = true;
            else
               if(low == "</inputs>")
                  in_inputs = false;

      if(!in_inputs || StringLen(line) == 0 || StringGetCharacter(line, 0) == '<')
         continue;

      int eq = StringFind(line, "=");
      if(eq <= 0)
         continue;
      string key = StringSubstr(line, 0, eq);
      string val = StringSubstr(line, eq + 1);
      StringTrimRight(key);
      StringTrimLeft(val);

      string lowkey = key;
      StringToLower(lowkey);
      if(magic == 0 && StringFind(lowkey, "magic") >= 0)
         magic = StringToInteger(val);

      if(count > 0)
         json += ",";
      json += StringFormat("{\"k\":\"%s\",\"v\":\"%s\"}",
                           JsonEscape(key), JsonEscape(val));
      count++;
     }
   FileClose(fh);
   FileDelete(file);
   return(true);
  }

void ChartProbe(const long id, const string expert, long &magic, string &inputs)
  {
   magic  = 0;
   inputs = "";
   if(!ReportInputs || expert == "")
      return;

   for(int i = 0; i < ArraySize(g_pm_chart); i++)
      if(g_pm_chart[i] == id && g_pm_expert[i] == expert)
        {
         magic  = g_pm_magic[i];           // recomputed only when the EA changes
         inputs = g_pm_inputs[i];
         return;
        }

   ReadInputsFromTemplate(id, magic, inputs);

   int n = ArraySize(g_pm_chart);
   ArrayResize(g_pm_chart, n + 1);
   ArrayResize(g_pm_expert, n + 1);
   ArrayResize(g_pm_magic, n + 1);
   ArrayResize(g_pm_inputs, n + 1);
   g_pm_chart[n]  = id;
   g_pm_expert[n] = expert;
   g_pm_magic[n]  = magic;
   g_pm_inputs[n] = inputs;
   if(VerboseLog)
      PrintFormat("settings for %s on chart %I64d: magic %s", expert, id,
                  magic > 0 ? (string)magic : "not found");
  }

//+------------------------------------------------------------------+
void WriteStatus()
  {
   string rows = "";
   long   id   = ChartFirst();
   int    n    = 0;

   while(id >= 0)
     {
      string expert = ChartGetString(id, CHART_EXPERT_NAME);
      string symbol = ChartSymbol(id);
      long   period = ChartPeriod(id);

      long   magic  = 0;
      string inputs = "";
      if(id != g_self_chart)
         ChartProbe(id, expert, magic, inputs);

      if(n > 0)
         rows += ",";
      rows += StringFormat(
                 "{\"chart\":%I64d,\"symbol\":\"%s\",\"period\":%I64d,\"expert\":\"%s\","
                 "\"magic\":%I64d,\"inputs\":[%s],\"is_manager\":%s}",
                 id, JsonEscape(symbol), period, JsonEscape(expert), magic, inputs,
                 (id == g_self_chart) ? "true" : "false");
      n++;
      id = ChartNext(id);
     }

   string paused[];
   int pcount = LoadPaused(paused);
   string prows = "";
   for(int i = 0; i < pcount; i++)
     {
      if(i > 0)
         prows += ",";
      prows += StringFormat("{\"key\":\"%s\",\"symbol\":\"%s\",\"period\":%d,\"expert\":\"%s\"}",
                            JsonEscape(Part(paused[i], 0)), JsonEscape(Part(paused[i], 1)),
                            (int)StringToInteger(Part(paused[i], 2)), JsonEscape(Part(paused[i], 3)));
     }

   string json = StringFormat(
                    "{\"at\":\"%s\",\"login\":%I64d,\"algo_trading\":%s,\"control_allowed\":%s,"
                    "\"charts\":[%s],\"paused\":[%s]}",
                    TimeToString(TimeGMT(), TIME_DATE | TIME_SECONDS),
                    AccountInfoInteger(ACCOUNT_LOGIN),
                    TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) ? "true" : "false",
                    AllowControl ? "true" : "false",
                    rows, prows);

   int fh = FileOpen(STATUS_FILE, FILE_WRITE | FILE_TXT | FILE_ANSI);
   if(fh == INVALID_HANDLE)
      return;
   FileWriteString(fh, json);
   FileClose(fh);
  }

//+------------------------------------------------------------------+
string GetField(const string text, const string key)
  {
   string lines[];
   int count = StringSplit(text, (ushort)'\n', lines);
   for(int i = 0; i < count; i++)
     {
      string line = lines[i];
      StringTrimLeft(line);
      StringTrimRight(line);
      int eq = StringFind(line, "=");
      if(eq <= 0)
         continue;
      if(StringSubstr(line, 0, eq) == key)
         return(StringSubstr(line, eq + 1));
     }
   return("");
  }

void WriteResult(const string id, const bool ok, const string message)
  {
   int fh = FileOpen(RESULT_FILE, FILE_WRITE | FILE_TXT | FILE_ANSI);
   if(fh != INVALID_HANDLE)
     {
      FileWriteString(fh, StringFormat("id=%s\nok=%s\nmessage=%s\n", id, ok ? "1" : "0", message));
      FileClose(fh);
     }
   if(VerboseLog)
      PrintFormat("FxeaManager: %s -> %s (%s)", id, ok ? "ok" : "failed", message);
  }

//+------------------------------------------------------------------+
long FindChart(const long want_id, const string want_symbol, const string want_expert, int &matches)
  {
   matches    = 0;
   long found = -1;
   long id    = ChartFirst();

   while(id >= 0)
     {
      if(id != g_self_chart)                    // never target ourselves
        {
         bool hit = true;
         if(want_id > 0 && id != want_id)
            hit = false;
         if(hit && want_symbol != "" && ChartSymbol(id) != want_symbol)
            hit = false;
         if(hit && want_expert != "" && ChartGetString(id, CHART_EXPERT_NAME) != want_expert)
            hit = false;
         if(hit && want_id <= 0 && ChartGetString(id, CHART_EXPERT_NAME) == "")
            hit = false;
         if(hit)
           {
            matches++;
            if(found < 0)
               found = id;
           }
        }
      id = ChartNext(id);
     }
   return(found);
  }

//+------------------------------------------------------------------+
//| pause: remember the chart as a template, then close it            |
//+------------------------------------------------------------------+
void DoPause(const string id, const long chart, const string symbol, const string expert)
  {
   int  matches = 0;
   long target  = FindChart(chart, symbol, expert, matches);
   if(target < 0)
     {
      WriteResult(id, false, "no chart matched");
      return;
     }
   if(matches > 1 && chart <= 0)
     {
      WriteResult(id, false, StringFormat("%d charts matched - pass an explicit chart id", matches));
      return;
     }

   string sym = ChartSymbol(target);
   long   per = ChartPeriod(target);
   string exp = ChartGetString(target, CHART_EXPERT_NAME);
   string key = StringFormat("%I64d", target);
   string tpl = "fxea_paused_" + key;

   // the template carries the EA and its inputs; without it, resume would only
   // reopen an empty chart
   if(!ChartSaveTemplate(target, tpl))
     {
      WriteResult(id, false, StringFormat("could not save template (error %d) - EA left running", GetLastError()));
      return;
     }

   string paused[];
   int n = LoadPaused(paused);
   ArrayResize(paused, n + 1);
   paused[n] = StringFormat("%s|%s|%I64d|%s|%s", key, sym, per, exp, tpl);
   SavePaused(paused);

   if(ChartClose(target))
      WriteResult(id, true, StringFormat("paused %s on %s (resume restores its settings)", exp, sym));
   else
      WriteResult(id, false, StringFormat("ChartClose failed (error %d)", GetLastError()));
   WriteStatus();
  }

//+------------------------------------------------------------------+
//| run: reopen the chart and re-apply the saved template             |
//+------------------------------------------------------------------+
void DoRun(const string id, const string key)
  {
   string paused[];
   int n = LoadPaused(paused);
   int at = -1;
   for(int i = 0; i < n; i++)
      if(Part(paused[i], 0) == key)
        {
         at = i;
         break;
        }
   if(at < 0)
     {
      WriteResult(id, false, "nothing paused under key " + key);
      return;
     }

   string sym = Part(paused[at], 1);
   long   per = (long)StringToInteger(Part(paused[at], 2));
   string exp = Part(paused[at], 3);
   string tpl = Part(paused[at], 4);

   long fresh = ChartOpen(sym, (ENUM_TIMEFRAMES)per);
   if(fresh == 0)
     {
      WriteResult(id, false, StringFormat("ChartOpen failed for %s (error %d)", sym, GetLastError()));
      return;
     }
   if(!ChartApplyTemplate(fresh, tpl + ".tpl"))
     {
      WriteResult(id, false, StringFormat("chart opened but template %s failed (error %d)",
                                          tpl, GetLastError()));
      return;
     }

   // drop the record only once the EA is actually back
   string keep[];
   for(int i = 0; i < n; i++)
      if(i != at)
        {
         int m = ArraySize(keep);
         ArrayResize(keep, m + 1);
         keep[m] = paused[i];
        }
   SavePaused(keep);

   WriteResult(id, true, StringFormat("running %s on %s again", exp, sym));
   WriteStatus();
  }

//+------------------------------------------------------------------+
//| Change an EA settings by rewriting its template and reapplying it.|
//|                                                                   |
//| MT5 has no call to set another EA inputs, so this saves the chart |
//| template, edits the values inside its <inputs> block and applies  |
//| it back. The EA reloads: its open trades stay untouched, but any  |
//| state it kept in memory starts over, which is why an EA holding   |
//| positions is refused unless the caller insists.                   |
//+------------------------------------------------------------------+
int CountOpenFor(const long magic)
  {
   // positions only: a pending order carries no EA state, it just sits at its
   // price, so it is a warning on the page rather than a reason to refuse
   int n = 0;
   if(magic <= 0)
      return(0);
   for(int i = PositionsTotal() - 1; i >= 0; i--)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket > 0 && PositionGetInteger(POSITION_MAGIC) == magic)
         n++;
     }
   return(n);
  }

void ForgetProbe(const long id)
  {
   for(int i = 0; i < ArraySize(g_pm_chart); i++)
      if(g_pm_chart[i] == id)
         g_pm_expert[i] = "";               // forces a re-read after the reload
  }

void DoSetInputs(const string id, const long chart, const bool force)
  {
   if(!AllowEditInputs)
     {
      WriteResult(id, false, "editing settings is switched off in this EA inputs");
      return;
     }

   string expert = ChartGetString(chart, CHART_EXPERT_NAME);
   if(chart <= 0 || chart == g_self_chart || expert == "")
     {
      WriteResult(id, false, "no EA on that chart");
      return;
     }

   // what to change, written next to the command so long lists are no problem
   string keys[], vals[];
   int    n = 0;
   int    fh = FileOpen(INPUTS_FILE, FILE_READ | FILE_TXT | FILE_ANSI);
   if(fh == INVALID_HANDLE)
     {
      WriteResult(id, false, "no settings file to apply");
      return;
     }
   while(!FileIsEnding(fh))
     {
      string line = FileReadString(fh);
      StringTrimLeft(line);
      StringTrimRight(line);
      int eq = StringFind(line, "=");
      if(eq <= 0)
         continue;
      ArrayResize(keys, n + 1);
      ArrayResize(vals, n + 1);
      keys[n] = StringSubstr(line, 0, eq);
      vals[n] = StringSubstr(line, eq + 1);
      n++;
     }
   FileClose(fh);
   FileDelete(INPUTS_FILE);
   if(n == 0)
     {
      WriteResult(id, false, "nothing to change");
      return;
     }

   long   magic = 0;
   string dump  = "";
   ChartProbe(chart, expert, magic, dump);
   int held = CountOpenFor(magic);
   if(held > 0 && !force)
     {
      WriteResult(id, false, StringFormat(
                     "%s has %d open position(s) on magic %I64d - reloading it drops the state it manages them with.",
                     expert, held, magic));
      return;
     }

   string file = EDIT_NAME + ".tpl";
   if(FileIsExist(file))
      FileDelete(file);
   if(!ChartSaveTemplate(chart, "\\Files\\" + EDIT_NAME))
     {
      WriteResult(id, false, "could not save the chart template");
      return;
     }
   for(int wait = 0; wait < 20 && !FileIsExist(file); wait++)
      Sleep(50);

   string lines[];
   int    count = 0;
   fh = FileOpen(file, FILE_READ | FILE_TXT | FILE_ANSI);
   if(fh == INVALID_HANDLE)
     {
      WriteResult(id, false, "could not read the saved template");
      return;
     }
   while(!FileIsEnding(fh))
     {
      ArrayResize(lines, count + 1);
      lines[count] = FileReadString(fh);
      count++;
     }
   FileClose(fh);

   bool in_expert = false, in_inputs = false;
   int  applied   = 0;
   for(int i = 0; i < count; i++)
     {
      string low = lines[i];
      StringTrimLeft(low);
      StringTrimRight(low);
      StringToLower(low);
      if(low == "<expert>")
         in_expert = true;
      if(low == "</expert>")
         in_expert = false;
      if(in_expert && low == "<inputs>")
         in_inputs = true;
      if(low == "</inputs>")
         in_inputs = false;
      if(!in_inputs)
         continue;

      int eq = StringFind(lines[i], "=");
      if(eq <= 0)
         continue;
      string key = StringSubstr(lines[i], 0, eq);
      StringTrimLeft(key);
      StringTrimRight(key);
      for(int j = 0; j < n; j++)
         if(keys[j] == key)
           {
            lines[i] = key + "=" + vals[j];
            applied++;
            break;
           }
     }
   if(applied == 0)
     {
      WriteResult(id, false, "none of those settings exist on this EA");
      return;
     }

   fh = FileOpen(file, FILE_WRITE | FILE_TXT | FILE_ANSI);
   if(fh == INVALID_HANDLE)
     {
      WriteResult(id, false, "could not rewrite the template");
      return;
     }
   for(int i = 0; i < count; i++)
      FileWriteString(fh, lines[i] + "\n");
   FileClose(fh);

   if(!ChartApplyTemplate(chart, "\\Files\\" + EDIT_NAME))
     {
      WriteResult(id, false, StringFormat("ChartApplyTemplate failed (error %d)", GetLastError()));
      return;
     }
   ForgetProbe(chart);
   WriteResult(id, true, StringFormat("%d setting(s) applied - %s reloaded%s",
                                      applied, expert, held > 0 ? " while holding trades" : ""));
   WriteStatus();
  }

//+------------------------------------------------------------------+
void ProcessCommand()
  {
   if(!FileIsExist(CMD_FILE))
      return;

   int fh = FileOpen(CMD_FILE, FILE_READ | FILE_TXT | FILE_ANSI);
   if(fh == INVALID_HANDLE)
      return;
   string body = "";
   while(!FileIsEnding(fh))
      body += FileReadString(fh) + "\n";
   FileClose(fh);
   FileDelete(CMD_FILE);                        // consume it, so it runs once

   string id     = GetField(body, "id");
   string action = GetField(body, "action");
   string symbol = GetField(body, "symbol");
   string expert = GetField(body, "expert");
   string key    = GetField(body, "key");
   long   chart  = (long)StringToInteger(GetField(body, "chart"));

   if(action == "status")
     {
      WriteStatus();
      WriteResult(id, true, "status refreshed");
      return;
     }

   if(!AllowControl && (action == "pause" || action == "run" || action == "unload" || action == "setinputs"))
     {
      WriteResult(id, false, "control disabled in this EA inputs");
      return;
     }

   if(action == "pause")
     {
      DoPause(id, chart, symbol, expert);
      return;
     }

   if(action == "run" || action == "resume")
     {
      DoRun(id, key != "" ? key : StringFormat("%I64d", chart));
      return;
     }

   if(action == "setinputs")
     {
      DoSetInputs(id, chart, GetField(body, "force") == "1");
      return;
     }

   if(action == "unload")
     {
      // kept for completeness: closes the chart WITHOUT saving a template, so it
      // cannot be resumed from here
      int  matches = 0;
      long target  = FindChart(chart, symbol, expert, matches);
      if(target < 0)
        {
         WriteResult(id, false, "no chart matched");
         return;
        }
      if(matches > 1 && chart <= 0)
        {
         WriteResult(id, false, StringFormat("%d charts matched - pass an explicit chart id", matches));
         return;
        }
      if(ChartClose(target))
         WriteResult(id, true, "chart closed (not resumable - use pause instead)");
      else
         WriteResult(id, false, StringFormat("ChartClose failed (error %d)", GetLastError()));
      WriteStatus();
      return;
     }

   WriteResult(id, false, "unknown action: " + action);
  }
//+------------------------------------------------------------------+
