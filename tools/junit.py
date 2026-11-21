import xml.etree.ElementTree as ET, re
def failed_ids(xml_path):
    out = set()
    try:
        root = ET.parse(xml_path).getroot()
    except Exception:
        return None
    finally:
        import os
        try: os.remove(xml_path)
        except OSError: pass
    for tc in root.iter("testcase"):
        if any(c.tag in ("failure", "error") for c in tc):
            cls = tc.get("classname", ""); name = re.split(r"[\s\[(]", tc.get("name", ""), 1)[0]
            parts = cls.split(".")
            if len(parts) >= 2:
                out.add("/".join(parts[:-1]) + ".py::" + parts[-1] + "::" + name)
    return out
