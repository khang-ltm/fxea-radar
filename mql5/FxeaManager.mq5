//+------------------------------------------------------------------+
//|  FxeaManager.mq5 - chart manager for the FX EA Radar agent        |
//|                                                                   |
//|  Attach ONCE to a spare chart (any symbol, any timeframe). It     |
//|  never trades: it reports which EA sits on which chart, and can   |
//|  unload an EA by closing its chart - the only mechanism MT5 gives |
//|  for stopping a compiled third-party EA from outside.             |
//|                                                                   |
//|  Talks to the Python agent through files in MQL5\Files, so there  |
//|  is no WebRequest permission to grant, no network hop, and no     |
//|  token inside MQL5:                                               |
//|      fxea_cmd.txt     <- written by the agent, one command        |
//|      fxea_status.json -> written here, refreshed every few sec    |
//|      fxea_result.txt  -> written here after a command runs        |
//|                                                                   |
//|  Driven by a timer rather than OnTick, so it still responds on a  |
//|  quiet symbol or when the market is closed.                       |
//+------------------------------------------------------------------+
#property copyright "FX EA Radar"
#property version   "1.00"
#property strict

input int  TimerSeconds    = 1;      // how often to poll for a command
input int  StatusEverySecs = 5;      // how often to rewrite the status file
input bool AllowUnload     = true;   // master switch for chart closing
input bool VerboseLog      = true;   // print actions to the Experts log

#define CMD_FILE    "fxea_cmd.txt"
#define RESULT_FILE "fxea_result.txt"
#define STATUS_FILE "fxea_status.json"

datetime g_last_status = 0;
long     g_self_chart  = 0;

//+------------------------------------------------------------------+
int OnInit()
  {
   g_self_chart = ChartID();
   EventSetTimer(MathMax(1, TimerSeconds));
   if(VerboseLog)
      PrintFormat("FxeaManager on chart %I64d (%s %s). Unload allowed: %s",
                  g_self_chart, _Symbol, EnumToString((ENUM_TIMEFRAMES)_Period),
                  AllowUnload ? "yes" : "no");
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
//| Escape what would otherwise break the JSON this EA emits.         |
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
//| Snapshot of every open chart and the EA attached to it.           |
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

      if(n > 0)
         rows += ",";
      rows += StringFormat(
                 "{\"chart\":%I64d,\"symbol\":\"%s\",\"period\":%I64d,\"expert\":\"%s\",\"is_manager\":%s}",
                 id, JsonEscape(symbol), period, JsonEscape(expert),
                 (id == g_self_chart) ? "true" : "false");
      n++;
      id = ChartNext(id);
     }

   string json = StringFormat(
                    "{\"at\":\"%s\",\"login\":%I64d,\"algo_trading\":%s,\"unload_allowed\":%s,\"charts\":[%s]}",
                    TimeToString(TimeGMT(), TIME_DATE | TIME_SECONDS),
                    AccountInfoInteger(ACCOUNT_LOGIN),
                    TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) ? "true" : "false",
                    AllowUnload ? "true" : "false",
                    rows);

   int fh = FileOpen(STATUS_FILE, FILE_WRITE | FILE_TXT | FILE_ANSI);
   if(fh == INVALID_HANDLE)
     {
      if(VerboseLog)
         PrintFormat("FxeaManager: cannot write %s (error %d)", STATUS_FILE, GetLastError());
      return;
     }
   FileWriteString(fh, json);
   FileClose(fh);
  }

//+------------------------------------------------------------------+
//| One "key=value" per line. Deliberately not JSON: MQL5 has no      |
//| parser, and this side consumes only a fixed, tiny schema.         |
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
      FileWriteString(fh, StringFormat("id=%s\nok=%s\nmessage=%s\n",
                                       id, ok ? "1" : "0", message));
      FileClose(fh);
     }
   if(VerboseLog)
      PrintFormat("FxeaManager: %s -> %s (%s)", id, ok ? "ok" : "failed", message);
  }

//+------------------------------------------------------------------+
//| Locate a chart by explicit id, or by symbol plus EA name.         |
//+------------------------------------------------------------------+
long FindChart(const long want_id, const string want_symbol, const string want_expert, int &matches)
  {
   matches   = 0;
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
         // a chart with no EA is only a target when asked for by id
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
   long   chart  = (long)StringToInteger(GetField(body, "chart"));

   if(action == "status")
     {
      WriteStatus();
      WriteResult(id, true, "status refreshed");
      return;
     }

   if(action == "unload")
     {
      if(!AllowUnload)
        {
         WriteResult(id, false, "unload disabled in this EA inputs");
         return;
        }
      int  matches = 0;
      long target  = FindChart(chart, symbol, expert, matches);
      if(target < 0)
        {
         WriteResult(id, false, "no chart matched");
         return;
        }
      if(matches > 1 && chart <= 0)
        {
         // refuse to guess: closing the wrong EA cannot be undone from here
         WriteResult(id, false, StringFormat("%d charts matched - pass an explicit chart id", matches));
         return;
        }
      string what = StringFormat("chart %I64d (%s, %s)", target, ChartSymbol(target),
                                 ChartGetString(target, CHART_EXPERT_NAME));
      if(ChartClose(target))
         WriteResult(id, true, "unloaded " + what);
      else
         WriteResult(id, false, StringFormat("ChartClose failed on %s (error %d)", what, GetLastError()));
      WriteStatus();
      return;
     }

   if(action == "pause" || action == "resume")
     {
      // Cooperative only: a compiled third-party EA cannot read this. It exists
      // for EAs whose source you control, and costs nothing to support.
      string magic = GetField(body, "magic");
      string var   = "fxea_pause_" + magic;
      GlobalVariableSet(var, (action == "pause") ? 1.0 : 0.0);
      WriteResult(id, true, StringFormat("%s set for magic %s", var, magic));
      return;
     }

   WriteResult(id, false, "unknown action: " + action);
  }
//+------------------------------------------------------------------+
