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
#property version   "1.27"
#property strict

input int  TimerSeconds    = 1;      // how often to poll for a command
input int  StatusEverySecs = 5;      // how often to rewrite the status file
input bool AllowControl    = true;   // master switch for pause / run
input bool AllowEditInputs = true;   // let the page change EA settings
input bool AllowAttach     = true;   // let the page put an EA on a new chart
input bool VerboseLog      = true;   // print actions to the Experts log
input bool ReportInputs    = true;   // report each EA settings (magic, lots, ...)

#define CMD_FILE     "fxea_cmd.txt"
#define RESULT_FILE  "fxea_result.txt"
#define STATUS_FILE  "fxea_status.json"
#define PAUSED_FILE  "fxea_paused.txt"
#define INPUTS_FILE  "fxea_inputs.txt"
#define EDIT_NAME    "fxea_edit"
#define ATTACH_NAME  "fxea_attach"

datetime g_last_status = 0;
long     g_self_chart  = 0;

//+------------------------------------------------------------------+
int OnInit()
  {
   g_self_chart = ChartID();
   EventSetTimer(MathMax(1, TimerSeconds));
   if(VerboseLog)
      PrintFormat("FxeaManager %s on chart %I64d (%s). Control allowed: %s",
                  "1.27", g_self_chart, _Symbol, AllowControl ? "yes" : "no");
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
int    g_pm_mode[];

bool ReadInputsFromTemplate(const long id, long &magic, string &json, int &mode)
  {
   magic = 0;
   json  = "";
   mode  = 0;

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

      // expertmode is how MT5 records per-EA permissions - including whether algo
      // trading is allowed for it. A template written without it loads the EA with
      // trading switched off, which is what every attach from the page did.
      if(in_expert && StringFind(low, "expertmode=") == 0)
         mode = (int)StringToInteger(StringSubstr(line, 11));

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

void ChartProbe(const long id, const string expert, long &magic, string &inputs, int &mode)
  {
   magic  = 0;
   inputs = "";
   mode   = 0;
   if(!ReportInputs || StringLen(expert) == 0)
      return;

   for(int i = 0; i < ArraySize(g_pm_chart); i++)
      if(g_pm_chart[i] == id && g_pm_expert[i] == expert)
        {
         magic  = g_pm_magic[i];           // recomputed only when the EA changes
         inputs = g_pm_inputs[i];
         mode   = g_pm_mode[i];
         return;
        }

   ReadInputsFromTemplate(id, magic, inputs, mode);

   int n = ArraySize(g_pm_chart);
   ArrayResize(g_pm_chart, n + 1);
   ArrayResize(g_pm_expert, n + 1);
   ArrayResize(g_pm_magic, n + 1);
   ArrayResize(g_pm_inputs, n + 1);
   ArrayResize(g_pm_mode, n + 1);
   g_pm_chart[n]  = id;
   g_pm_expert[n] = expert;
   g_pm_magic[n]  = magic;
   g_pm_inputs[n] = inputs;
   g_pm_mode[n]   = mode;
   if(VerboseLog)
      PrintFormat("settings for %s on chart %I64d: magic %s",
                  StringLen(expert) > 0 ? expert : "(no EA)", id,
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
      if(StringLen(expert) == 0)
         expert = "";                   // a NULL string prints as "(null)"
      string symbol = ChartSymbol(id);
      if(StringLen(symbol) == 0)
         symbol = "";
      long   period = ChartPeriod(id);

      long   magic  = 0;
      string inputs = "";
      int    mode   = 0;
      if(id != g_self_chart)
         ChartProbe(id, expert, magic, inputs, mode);

      if(n > 0)
         rows += ",";
      rows += StringFormat(
                 "{\"chart\":%I64d,\"symbol\":\"%s\",\"period\":%I64d,\"expert\":\"%s\","
                 "\"magic\":%I64d,\"expertmode\":%d,\"inputs\":[%s],\"is_manager\":%s}",
                 id, JsonEscape(symbol), period, JsonEscape(expert), magic, mode, inputs,
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
                    "{\"at\":\"%s\",\"version\":\"%s\",\"login\":%I64d,\"algo_trading\":%s,"
                    "\"control_allowed\":%s,\"charts\":[%s],\"paused\":[%s]}",
                    TimeToString(TimeGMT(), TIME_DATE | TIME_SECONDS), "1.27",
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
   if(chart <= 0 || chart == g_self_chart || StringLen(expert) == 0)
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
   int    dmode = 0;
   ChartProbe(chart, expert, magic, dump, dmode);
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

   bool in_expert = false, in_inputs = false, seen_inputs = false;
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
        {
         in_inputs   = true;
         seen_inputs = true;
        }
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
   // An EA attached from a template that carried no <inputs> has no parameter set
   // stored for its chart, so every template saved from it comes back without one
   // and there is nothing here to rewrite. Create the block: the values are the
   // EA's own defaults plus whatever was asked for, which is what the chart would
   // have held had it been attached by hand.
   if(applied == 0)
     {
      string rebuilt[];
      int written2 = 0;
      for(int i = 0; i < count; i++)
        {
         string low2 = lines[i];
         StringTrimLeft(low2);
         StringTrimRight(low2);
         StringToLower(low2);
         if(low2 == "<inputs>" || low2 == "</inputs>")
            continue;                          // drop the empty block, if any
         if(low2 == "</expert>")
           {
            ArrayResize(rebuilt, written2 + 1);
            rebuilt[written2++] = "<inputs>";
            for(int j = 0; j < n; j++)
              {
               ArrayResize(rebuilt, written2 + 1);
               rebuilt[written2++] = keys[j] + "=" + vals[j];
              }
            ArrayResize(rebuilt, written2 + 1);
            rebuilt[written2++] = "</inputs>";
            applied = n;
           }
         ArrayResize(rebuilt, written2 + 1);
         rebuilt[written2++] = lines[i];
        }
      if(applied > 0)
        {
         ArrayResize(lines, written2);
         for(int i = 0; i < written2; i++)
            lines[i] = rebuilt[i];
         count = written2;
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
//| Put an EA on a new chart.                                         |
//|                                                                   |
//| Again there is no call for it: an EA can only arrive on a chart    |
//| through a template. Rather than hand-write one - the format is    |
//| undocumented and version-dependent - this borrows a real template |
//| from the manager own chart and swaps the <expert> block for the   |
//| requested EA and its settings. Symbol and timeframe are forced    |
//| afterwards, since the borrowed template carries the manager ones. |
//+------------------------------------------------------------------+
bool ReadLines(const string file, string &lines[])
  {
   int fh = FileOpen(file, FILE_READ | FILE_TXT | FILE_ANSI);
   if(fh == INVALID_HANDLE)
      return(false);
   int n = 0;
   while(!FileIsEnding(fh))
     {
      ArrayResize(lines, n + 1);
      lines[n] = FileReadString(fh);
      n++;
     }
   FileClose(fh);
   return(true);
  }

bool WriteLines(const string file, const string &lines[])
  {
   int fh = FileOpen(file, FILE_WRITE | FILE_TXT | FILE_ANSI);
   if(fh == INVALID_HANDLE)
      return(false);
   for(int i = 0; i < ArraySize(lines); i++)
      FileWriteString(fh, lines[i] + "\n");
   FileClose(fh);
   return(true);
  }

/* fxea_inputs.txt, if the agent staged one, becomes the new EA settings */
string StagedInputs()
  {
   string out = "";
   if(!FileIsExist(INPUTS_FILE))
      return(out);
   string lines[];
   if(ReadLines(INPUTS_FILE, lines))
      for(int i = 0; i < ArraySize(lines); i++)
        {
         string line = lines[i];
         StringTrimLeft(line);
         StringTrimRight(line);
         if(StringFind(line, "=") > 0)
            out += line + "\n";
        }
   FileDelete(INPUTS_FILE);
   return(out);
  }

void DoAttach(const string id, const string expert, const string path,
              const string symbol, const long period)
  {
   if(!AllowAttach)
     {
      WriteResult(id, false, "attaching is switched off in this EA inputs");
      return;
     }
   if(expert == "" || symbol == "")
     {
      WriteResult(id, false, "expert and symbol are both required");
      return;
     }
   if(!SymbolSelect(symbol, true))
     {
      WriteResult(id, false, "unknown symbol at this broker: " + symbol);
      return;
     }

   // refuse a second copy of the same EA on the same symbol and timeframe: two
   // instances trading one setup is how an account doubles its risk by accident
   long scan = ChartFirst();
   while(scan >= 0)
     {
      if(ChartGetString(scan, CHART_EXPERT_NAME) == expert
         && ChartSymbol(scan) == symbol && ChartPeriod(scan) == period)
        {
         WriteResult(id, false, StringFormat("%s already runs on %s (chart %I64d)",
                                             expert, symbol, scan));
         return;
        }
      scan = ChartNext(scan);
     }

   // The agent has already written fxea_attach.tpl into Profiles\\Templates with
   // this EA in it. That split exists because MQL5 file functions can only write
   // inside MQL5\\Files, while ChartApplyTemplate only reads templates from
   // Profiles\\Templates - editing a template in Files and applying it from there
   // failed with error 4101 every single time, which is why no EA ever loaded.
   long target = ChartOpen(symbol, (ENUM_TIMEFRAMES)period);
   if(target == 0)
     {
      WriteResult(id, false, StringFormat("ChartOpen failed (error %d)", GetLastError()));
      return;
     }

   bool ready = false;
   for(int wait = 0; wait < 40 && !ready; wait++)
     {
      Sleep(100);
      ready = StringLen(ChartSymbol(target)) > 0;
     }
   if(!ready)
     {
      ChartClose(target);
      WriteResult(id, false, "the new chart never became usable");
      return;
     }

   if(!ChartApplyTemplate(target, ATTACH_NAME))
     {
      int err = GetLastError();
      ChartClose(target);
      WriteResult(id, false, StringFormat("ChartApplyTemplate failed (error %d)", err));
      return;
     }

   for(int wait = 0; wait < 60; wait++)
     {
      Sleep(100);
      if(StringLen(ChartGetString(target, CHART_EXPERT_NAME)) > 0)
         break;
     }
   if(StringLen(ChartGetString(target, CHART_EXPERT_NAME)) == 0)
     {
      ChartClose(target);                          // never leave a bare chart behind
      WriteResult(id, false, "the template applied but no EA appeared on the chart");
      return;
     }

   // an EA can load and then remove itself; the caller deserves to know
   string loaded = ChartGetString(target, CHART_EXPERT_NAME);
   Sleep(9000);            // they have been dropping off at five to eight seconds
   string still = ChartGetString(target, CHART_EXPERT_NAME);
   ForgetProbe(target);
   if(StringLen(still) == 0)
     {
      WriteResult(id, false, StringFormat(
                     "%s loaded on %s then removed itself - check the Experts log", loaded, symbol));
      WriteStatus();
      return;
     }
   WriteResult(id, true, StringFormat("attached %s to %s %s (chart %I64d)",
                                      still, symbol, EnumToString((ENUM_TIMEFRAMES)period), target));
   WriteStatus();
  }

/* Pausing keeps a template so Run can restore the EA. Discarding is the other
   answer: drop the entry and its template, leaving no chart and no row. It
   stops nothing - a paused EA is already stopped - so it needs no guard beyond
   AllowControl. */
void DoForget(const string id, const string key)
  {
   string paused[];
   int n  = LoadPaused(paused);
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

   string expert = Part(paused[at], 3);
   string tpl    = Part(paused[at], 4);
   if(tpl != "" && FileIsExist(tpl + ".tpl"))
      FileDelete(tpl + ".tpl");

   string kept[];
   int k = 0;
   for(int i = 0; i < n; i++)
     {
      if(i == at)
         continue;
      ArrayResize(kept, k + 1);
      kept[k++] = paused[i];
     }
   SavePaused(kept);

   WriteResult(id, true, StringFormat("discarded %s - its saved settings are gone", expert));
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

   if(!AllowControl && (action == "pause" || action == "run" || action == "unload" || action == "setinputs" || action == "attach" || action == "forget"))
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

   if(action == "forget")
     {
      DoForget(id, key != "" ? key : StringFormat("%I64d", chart));
      return;
     }

   if(action == "attach")
     {
      DoAttach(id, expert, GetField(body, "path"), symbol,
               (long)StringToInteger(GetField(body, "period")));
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
