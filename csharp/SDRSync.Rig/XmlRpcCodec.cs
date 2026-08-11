using System.Globalization;
using System.Xml.Linq;

namespace SDRSync.Rig;

/// <summary>
/// Minimal hand-rolled XML-RPC request/response encoder and decoder,
/// covering only the value types flrig's remote-control interface actually
/// uses: string, int (i4), boolean, and array. Built on
/// System.Xml.Linq (BCL) rather than a third-party XML-RPC library --
/// flrig's whole surface here is 6 methods with scalar/array parameters,
/// far smaller than the general-purpose XML-RPC spec a full library would
/// implement (structs, dateTime, base64, nested faults-with-structs
/// beyond what fault parsing itself needs), so hand-rolling the exact
/// slice needed is more precise than pulling in a low-traffic/thinly
/// maintained NuGet wrapper for it.
/// </summary>
public static class XmlRpcCodec
{
    public sealed record DecodedRequest(string MethodName, IReadOnlyList<object?> Parameters);

    public sealed class XmlRpcFaultException(int faultCode, string faultString)
        : Exception($"XML-RPC fault {faultCode}: {faultString}")
    {
        public int FaultCode { get; } = faultCode;
        public string FaultString { get; } = faultString;
    }

    public static string EncodeRequest(string methodName, IReadOnlyList<object?> parameters)
    {
        var methodCall = new XElement("methodCall", new XElement("methodName", methodName));
        if (parameters.Count > 0)
        {
            methodCall.Add(new XElement("params", parameters.Select(p => new XElement("param", EncodeValue(p)))));
        }

        return new XDocument(new XDeclaration("1.0", "UTF-8", null), methodCall).ToString(SaveOptions.DisableFormatting);
    }

    public static string EncodeResponse(object? result)
    {
        var methodResponse = new XElement(
            "methodResponse",
            new XElement("params", new XElement("param", EncodeValue(result))));
        return new XDocument(new XDeclaration("1.0", "UTF-8", null), methodResponse).ToString(SaveOptions.DisableFormatting);
    }

    public static string EncodeFault(int faultCode, string faultString)
    {
        var faultValue = new XElement(
            "struct",
            new XElement("member", new XElement("name", "faultCode"), new XElement("value", new XElement("i4", faultCode))),
            new XElement("member", new XElement("name", "faultString"), new XElement("value", new XElement("string", faultString))));
        var methodResponse = new XElement("methodResponse", new XElement("fault", new XElement("value", faultValue)));
        return new XDocument(new XDeclaration("1.0", "UTF-8", null), methodResponse).ToString(SaveOptions.DisableFormatting);
    }

    public static DecodedRequest DecodeRequest(string xml)
    {
        var root = XDocument.Parse(xml).Root ?? throw new FormatException("Empty XML-RPC request");
        var methodName = root.Element("methodName")?.Value ?? throw new FormatException("Missing methodName");
        var paramsEl = root.Element("params");
        var parameters = paramsEl?.Elements("param")
            .Select(p => DecodeValue(p.Element("value") ?? throw new FormatException("param missing value")))
            .ToList() ?? new List<object?>();
        return new DecodedRequest(methodName, parameters);
    }

    /// <summary>Decodes a methodResponse. Throws XmlRpcFaultException for a fault reply.</summary>
    public static object? DecodeResponse(string xml)
    {
        var root = XDocument.Parse(xml).Root ?? throw new FormatException("Empty XML-RPC response");

        var fault = root.Element("fault");
        if (fault is not null)
        {
            var faultValue = fault.Element("value") ?? throw new FormatException("fault missing value");
            var dict = (Dictionary<string, object?>)DecodeValue(faultValue)!;
            var code = dict.TryGetValue("faultCode", out var c) ? Convert.ToInt32(c, CultureInfo.InvariantCulture) : -1;
            var message = dict.TryGetValue("faultString", out var m) ? m?.ToString() ?? "" : "";
            throw new XmlRpcFaultException(code, message);
        }

        var paramEl = root.Element("params")?.Element("param") ?? throw new FormatException("Missing params/param");
        var valueEl = paramEl.Element("value") ?? throw new FormatException("param missing value");
        return DecodeValue(valueEl);
    }

    private static XElement EncodeValue(object? value)
    {
        return value switch
        {
            null => new XElement("value", new XElement("string", "")),
            string s => new XElement("value", new XElement("string", s)),
            int i => new XElement("value", new XElement("i4", i.ToString(CultureInfo.InvariantCulture))),
            bool b => new XElement("value", new XElement("boolean", b ? "1" : "0")),
            System.Collections.IEnumerable arr and not string =>
                new XElement("value", new XElement("array", new XElement("data", arr.Cast<object?>().Select(EncodeValue)))),
            _ => throw new NotSupportedException($"Unsupported XML-RPC value type {value.GetType()}"),
        };
    }

    private static object? DecodeValue(XElement valueEl)
    {
        var child = valueEl.Elements().FirstOrDefault();
        if (child is null)
        {
            // A valueless <value>text</value> defaults to string per the
            // XML-RPC spec.
            return valueEl.Value;
        }

        return child.Name.LocalName switch
        {
            "string" => child.Value,
            "int" or "i4" => int.Parse(child.Value, CultureInfo.InvariantCulture),
            "boolean" => child.Value.Trim() == "1",
            "double" => double.Parse(child.Value, CultureInfo.InvariantCulture),
            "array" => (child.Element("data")?.Elements("value") ?? Enumerable.Empty<XElement>())
                .Select(DecodeValue).ToArray(),
            "struct" => DecodeStruct(child),
            _ => child.Value,
        };
    }

    private static Dictionary<string, object?> DecodeStruct(XElement structEl)
    {
        var dict = new Dictionary<string, object?>();
        foreach (var member in structEl.Elements("member"))
        {
            var name = member.Element("name")?.Value ?? throw new FormatException("struct member missing name");
            var value = DecodeValue(member.Element("value") ?? throw new FormatException("struct member missing value"));
            dict[name] = value;
        }

        return dict;
    }
}
