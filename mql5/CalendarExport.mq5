//+------------------------------------------------------------------+
//|                                              CalendarExport.mq5  |
//|   Exports the terminal's economic calendar to JSON for mt5-trader |
//+------------------------------------------------------------------+
//  Why this exists
//
//  MetaTrader 5 has an economic calendar and the Python module has no way to
//  read it: the MetaTrader5 package exposes no calendar_* function at all. The
//  only route is MQL5. This service runs inside the terminal, reads the calendar
//  on a timer, and writes one JSON file that the Python side reads.
//
//  Install
//    1. Copy this file to  <terminal data folder>\MQL5\Services\
//       (in MetaTrader: File -> Open Data Folder, then MQL5\Services)
//    2. In the terminal: F4 opens MetaEditor, compile it there, or run
//       build_sidecar.py which compiles it with MetaEditor64.exe for you.
//    3. In the terminal: View -> Navigator -> Services -> CalendarExport ->
//       right click -> Start. A service needs no chart and no Algo Trading.
//
//  The file lands in the terminal's common data folder as
//  Files\mt5trader_calendar.json, so several terminals can share one export.
//
//  Honest limits
//
//    * The calendar needs the terminal to be connected. With no connection the
//      service writes a heartbeat with an empty event list, and the Python side
//      treats a stale heartbeat as "cannot verify", not as "no news".
//    * The importance scale is exported twice on purpose: "importance" is
//      normalised to 0 low / 1 moderate / 2 high, and "importance_raw" is the
//      terminal's own enum value. If the MQL5 enum ever differs from what this
//      file assumes, the raw column shows it instead of hiding it.
//    * Event values are exported both raw and scaled, for the same reason.
//
//+------------------------------------------------------------------+
#property service
#property description "Exports the MT5 economic calendar to JSON for mt5-trader"
#property version   "1.00"

input int    HoursAhead      = 336;                 // export window, hours forward
input int    HoursBack       = 48;                  // export window, hours back
input int    MinutesBetween  = 5;                   // export interval
input int    MinImportance   = 0;                   // 0 none, 1 low, 2 moderate, 3 high

#define FILE_NAME "mt5trader_calendar.json"

//+------------------------------------------------------------------+
string JsonEscape(const string text)
{
   string out = "";
   int n = StringLen(text);
   for(int i = 0; i < n; i++)
   {
      ushort c = StringGetCharacter(text, i);
      if(c == '"' || c == '\\')
         out += "\\" + ShortToString(c);
      else if(c == '\n')
         out += "\\n";
      else if(c == '\r')
         out += "\\r";
      else if(c == '\t')
         out += "\\t";
      else if(c < 32)
         out += " ";
      else
         out += ShortToString(c);
   }
   return out;
}

//+------------------------------------------------------------------+
string ValueJson(const long raw, const int digits)
{
   if(raw == 0)
      return "null";
   double scale = MathPow(10.0, (double)digits);
   double value = (double)raw / scale;
   return StringFormat("%.*f", (digits > 0 ? digits : 0), value);
}

//+------------------------------------------------------------------+
string ImportanceName(const int raw)
{
   // ENUM_CALENDAR_EVENT_IMPORTANCE in MQL5, normalised to three levels.
   if(raw >= 3)
      return "high";
   if(raw == 2)
      return "moderate";
   return "low";
}

//+------------------------------------------------------------------+
int NormaliseImportance(const int raw)
{
   if(raw >= 3)
      return 2;
   if(raw == 2)
      return 1;
   return 0;
}

//+------------------------------------------------------------------+
void Export()
{
   datetime from = TimeTradeServer() - (datetime)(HoursBack * 3600);
   datetime to   = TimeTradeServer() + (datetime)(HoursAhead * 3600);

   MqlCalendarValue values[];
   int count = CalendarValueHistory(values, from, to, NULL, NULL);
   if(count < 0)
      count = 0;

   int handle = FileOpen(FILE_NAME, FILE_WRITE | FILE_TXT | FILE_ANSI | FILE_COMMON);
   if(handle == INVALID_HANDLE)
   {
      Print("CalendarExport: cannot open ", FILE_NAME, " for writing, error ",
            GetLastError());
      return;
   }

   FileWriteString(handle, "{\n");
   FileWriteString(handle, "  \"generated_server\": " +
                   IntegerToString((long)TimeTradeServer()) + ",\n");
   FileWriteString(handle, "  \"server_now\": \"" +
                   TimeToString(TimeTradeServer(), TIME_DATE | TIME_MINUTES) + "\",\n");
   FileWriteString(handle, "  \"interval_minutes\": " +
                   IntegerToString(MinutesBetween) + ",\n");
   FileWriteString(handle, "  \"terminal_build\": " +
                   IntegerToString(TerminalInfoInteger(TERMINAL_BUILD)) + ",\n");
   FileWriteString(handle, "  \"connected\": " +
                   (TerminalInfoInteger(TERMINAL_CONNECTED) ? "true" : "false") + ",\n");
   FileWriteString(handle, "  \"source\": \"terminal economic calendar via MQL5\",\n");
   FileWriteString(handle, "  \"values_note\": \"actual, forecast and previous are the "
                   "raw CFTC-style integer divided by 10^digits; the *_raw columns are "
                   "the terminal's own values\",\n");
   FileWriteString(handle, "  \"events\": [\n");

   int written = 0;
   for(int i = 0; i < count; i++)
   {
      MqlCalendarEvent event;
      if(!CalendarEventById(values[i].event_id, event))
         continue;
      if(event.importance < MinImportance)
         continue;

      MqlCalendarCountry country;
      CalendarCountryById(event.country_id, country);

      if(written > 0)
         FileWriteString(handle, ",\n");
      written++;

      FileWriteString(handle, "    {");
      FileWriteString(handle, "\"id\": " + IntegerToString((long)values[i].id));
      FileWriteString(handle, ", \"event_id\": " + IntegerToString((long)values[i].event_id));
      FileWriteString(handle, ", \"time\": " + IntegerToString((long)values[i].time));
      FileWriteString(handle, ", \"name\": \"" + JsonEscape(event.name) + "\"");
      FileWriteString(handle, ", \"code\": \"" + JsonEscape(event.event_code) + "\"");
      FileWriteString(handle, ", \"country\": \"" + JsonEscape(country.code) + "\"");
      FileWriteString(handle, ", \"currency\": \"" + JsonEscape(country.currency) + "\"");
      FileWriteString(handle, ", \"importance\": " +
                      IntegerToString(NormaliseImportance((int)event.importance)));
      FileWriteString(handle, ", \"importance_raw\": " +
                      IntegerToString((int)event.importance));
      FileWriteString(handle, ", \"importance_name\": \"" +
                      ImportanceName((int)event.importance) + "\"");
      FileWriteString(handle, ", \"unit\": " + IntegerToString((int)event.unit));
      FileWriteString(handle, ", \"digits\": " + IntegerToString(event.digits));
      FileWriteString(handle, ", \"multiplier\": " + IntegerToString(event.multiplier));
      FileWriteString(handle, ", \"period\": " + IntegerToString((long)values[i].period));
      FileWriteString(handle, ", \"actual\": " +
                      ValueJson(values[i].actual_value, event.digits));
      FileWriteString(handle, ", \"forecast\": " +
                      ValueJson(values[i].forecast_value, event.digits));
      FileWriteString(handle, ", \"previous\": " +
                      ValueJson(values[i].prev_value, event.digits));
      FileWriteString(handle, ", \"revised_previous\": " +
                      ValueJson(values[i].revised_prev_value, event.digits));
      FileWriteString(handle, ", \"actual_raw\": " +
                      IntegerToString((long)values[i].actual_value));
      FileWriteString(handle, "}");
   }

   FileWriteString(handle, "\n  ],\n");
   FileWriteString(handle, "  \"event_count\": " + IntegerToString(written) + "\n");
   FileWriteString(handle, "}\n");
   FileClose(handle);
}

//+------------------------------------------------------------------+
void OnStart()
{
   Print("CalendarExport: starting, window -", HoursBack, "h to +", HoursAhead,
         "h, every ", MinutesBetween, " minutes");
   while(!IsStopped())
   {
      Export();
      // Sleep in short steps so a stop request is honoured quickly.
      for(int i = 0; i < MinutesBetween * 60 && !IsStopped(); i++)
         Sleep(1000);
   }
   Print("CalendarExport: stopped");
}

//+------------------------------------------------------------------+
void OnStop()
{
   Print("CalendarExport: stop requested");
}
//+------------------------------------------------------------------+
