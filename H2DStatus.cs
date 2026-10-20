// Read-only status logger for a Bambu Lab printer on the local network (written for the H2D).
// Connects to the printer's own MQTT broker (TLS, port 8883, user "bblp", password = the access code shown on the
// printer), subscribes to device/<serial>/report and logs the temperatures and print stage. The only thing it ever
// sends to the printer besides the subscription is an optional "pushall" (asks for a full status report); it sends
// no commands that change anything. Minimal MQTT 3.1.1 client so nothing has to be installed.
using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using System.Net.Security;
using System.Net.Sockets;
using System.Text;
using System.Web.Script.Serialization;

public static class H2DStatus
{
    static Dictionary<string, object> state = new Dictionary<string, object>();

    static void WriteLen(List<byte> b, int len)
    {
        do { int d = len % 128; len /= 128; if (len > 0) d |= 0x80; b.Add((byte)d); } while (len > 0);
    }
    static void WriteStr(List<byte> b, string s)
    {
        byte[] u = Encoding.UTF8.GetBytes(s); b.Add((byte)(u.Length >> 8)); b.Add((byte)(u.Length & 0xFF)); b.AddRange(u);
    }
    static void Send(Stream s, byte type, List<byte> body)
    {
        var p = new List<byte>(); p.Add(type); WriteLen(p, body.Count); p.AddRange(body);
        s.Write(p.ToArray(), 0, p.Count); s.Flush();
    }
    static byte[] ReadN(Stream s, int n)
    {
        byte[] buf = new byte[n]; int got = 0;
        while (got < n) { int r = s.Read(buf, got, n - got); if (r <= 0) throw new IOException("connection closed by the printer"); got += r; }
        return buf;
    }
    static int ReadLen(Stream s)
    {
        int mult = 1, val = 0;
        while (true) { int d = ReadN(s, 1)[0]; val += (d & 127) * mult; if ((d & 128) == 0) return val; mult *= 128; }
    }

    // Report messages are partial updates; merge them into one running picture.
    static void Merge(Dictionary<string, object> into, Dictionary<string, object> from)
    {
        foreach (var kv in from)
        {
            var sub = kv.Value as Dictionary<string, object>;
            if (sub != null && into.ContainsKey(kv.Key) && into[kv.Key] is Dictionary<string, object>) Merge((Dictionary<string, object>)into[kv.Key], sub);
            else into[kv.Key] = kv.Value;
        }
    }
    static object Get(string path)
    {
        object cur = state;
        foreach (string part in path.Split('.'))
        {
            var d = cur as Dictionary<string, object>;
            if (d != null) { if (!d.TryGetValue(part, out cur)) return null; continue; }
            var a = cur as object[]; int idx;
            if (a != null && int.TryParse(part, out idx) && idx < a.Length) { cur = a[idx]; continue; }
            return null;
        }
        return cur;
    }
    static string Txt(string path) { object o = Get(path); return o == null ? "" : Convert.ToString(o, System.Globalization.CultureInfo.InvariantCulture); }
    // H2-series temperatures are packed: current in the low 16 bits, target in the high 16.
    static string Packed(string path)
    {
        object o = Get(path); if (o == null) return "";
        long v; try { v = Convert.ToInt64(o); } catch { return Txt(path); }
        return (v & 0xFFFF) + "/" + ((v >> 16) & 0xFFFF);
    }

    public static void Log(string ip, string serial, string accessCode, string outDir, int minutes, int everySeconds, bool pushAll)
    {
        string csv = Path.Combine(outDir, "status-log.csv"), latest = Path.Combine(outDir, "status-latest.json");
        if (!File.Exists(csv)) File.WriteAllText(csv, "time,state,stage,percent,remaining_min,layer,chamber_cur/target,chamber_old,bed_cur/target,bed_old,nozzle_old\r\n");
        using (var tcp = new TcpClient())
        {
            tcp.Connect(ip, 8883);
            // The printer presents Bambu's own certificate, not one a PC trusts by default; accept it (local network only).
            using (var ssl = new SslStream(tcp.GetStream(), false, (a, b, c, d) => true))
            {
                ssl.AuthenticateAsClient(ip);
                var body = new List<byte>(); WriteStr(body, "MQTT"); body.Add(4); body.Add(0xC2); body.Add(0); body.Add(60);
                WriteStr(body, "h2dstatus-" + Guid.NewGuid().ToString("N").Substring(0, 8)); WriteStr(body, "bblp"); WriteStr(body, accessCode);
                Send(ssl, 0x10, body);
                byte[] ack = ReadN(ssl, 4);
                if (ack[0] != 0x20 || ack[3] != 0) throw new Exception("The printer refused the connection (code " + ack[3] + "). 4 or 5 = wrong access code or not allowed in this mode.");
                Console.WriteLine("Connected to the printer.");
                body = new List<byte>(); body.Add(0); body.Add(1); WriteStr(body, "device/" + serial + "/report"); body.Add(0);
                Send(ssl, 0x82, body);
                if (pushAll)
                {
                    body = new List<byte>(); WriteStr(body, "device/" + serial + "/request");
                    body.AddRange(Encoding.UTF8.GetBytes("{\"pushing\":{\"sequence_id\":\"0\",\"command\":\"pushall\"}}"));
                    Send(ssl, 0x30, body);
                }
                var js = new JavaScriptSerializer(); js.MaxJsonLength = int.MaxValue;
                DateTime end = DateTime.Now.AddMinutes(minutes), nextLog = DateTime.Now.AddSeconds(3), nextPing = DateTime.Now.AddSeconds(30);
                int messages = 0;
                while (DateTime.Now < end)
                {
                    if (tcp.Client.Poll(500000, SelectMode.SelectRead))
                    {
                        byte head = ReadN(ssl, 1)[0]; int len = ReadLen(ssl); byte[] data = ReadN(ssl, len);
                        if ((head & 0xF0) == 0x30)
                        {
                            int tl = (data[0] << 8) | data[1], off = 2 + tl + (((head >> 1) & 3) > 0 ? 2 : 0);
                            string json = Encoding.UTF8.GetString(data, off, data.Length - off);
                            try { var d = js.DeserializeObject(json) as Dictionary<string, object>; if (d != null) { Merge(state, d); messages++; } } catch { }
                        }
                    }
                    if (DateTime.Now >= nextPing) { ssl.Write(new byte[] { 0xC0, 0 }, 0, 2); ssl.Flush(); nextPing = DateTime.Now.AddSeconds(30); }
                    if (DateTime.Now >= nextLog)
                    {
                        nextLog = DateTime.Now.AddSeconds(everySeconds);
                        string line = string.Join(",", new[] { DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss"), Txt("print.gcode_state"), Txt("print.stg_cur"), Txt("print.mc_percent"),
                            Txt("print.mc_remaining_time"), Txt("print.layer_num"), Packed("print.device.ctc.info.temp"), Txt("print.chamber_temper"),
                            Packed("print.device.bed.info.temp"), Txt("print.bed_temper"), Txt("print.nozzle_temper") });
                        File.AppendAllText(csv, line + "\r\n");
                        File.WriteAllText(latest, js.Serialize(state));
                        Console.WriteLine(line + "   (" + messages + " messages so far)");
                    }
                }
            }
        }
    }
}
