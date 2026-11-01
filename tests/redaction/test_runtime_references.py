import json
import pytest
from dma.redaction.references import redactor_from_references,redactor_from_config

def test_dotenv_and_json_runtime_values_redacted_before_capture(tmp_path):
 secret="SYNTHETIC-MIMO-BARE-51-character-value-for-runtime-check"
 env=tmp_path/"host.env";env.write_text(f'export MIMO_API_KEY="{secret}" # old tool output\nPASS=hash#inside\n')
 key=tmp_path/"gateway.json";key.write_text(json.dumps({"client":{"key":"SYNTHETIC-GATEWAY-KEY"}}))
 refs=[{"file":str(env),"format":"dotenv","names":["MIMO_API_KEY","PASS"]},{"file":str(key),"format":"json","path":["client","key"]}]
 cfg=tmp_path/"runtime.json";cfg.write_text(json.dumps({"redaction_secret_refs":refs}))
 red=redactor_from_config(cfg);result=red.redact(secret+" and SYNTHETIC-GATEWAY-KEY and hash#inside")
 assert result.replacements==3 and all(v not in result.text for v in [secret,"SYNTHETIC-GATEWAY-KEY","hash#inside"])
 assert result.credential_refs and result.contract()["policy_version"]=="redact:1"
 assert secret not in cfg.read_text() and "SYNTHETIC-GATEWAY-KEY" not in cfg.read_text()

@pytest.mark.parametrize("mode",["missing_file","missing_key","unsupported","invalid_json","interpolation"])
def test_reference_failure_never_echoes_material(tmp_path,mode):
 secret="SYNTHETIC-RAW-SECRET-NOT-IN-ERROR";p=tmp_path/"config";ref={"file":str(p),"format":"dotenv","names":["KEY"]}
 if mode=="missing_file":pass
 elif mode=="missing_key":p.write_text("OTHER="+secret)
 elif mode=="unsupported":p.write_text(secret);ref["format"]="unknown"
 elif mode=="invalid_json":p.write_text(secret);ref={"file":str(p),"format":"json","path":["key"]}
 else:p.write_text("KEY=$"+"{UNRESOLVED_SECRET}")
 with pytest.raises(ValueError,match="^REDACTION_REFERENCE_UNAVAILABLE$") as exc:redactor_from_references([ref])
 assert secret not in str(exc.value)

def test_yaml_runtime_reference_explicit_nested_paths_only(tmp_path):
 p=tmp_path/"credentials.yaml"
 secret="SYNTHETIC-YAML-BARE-KEY-123456789"
 browser="SYNTHETIC-YAML-BROWSER-123456789"
 p.write_text("refs:\n  API_KEY: "+secret+"\nrecords:\n  client-connection/browser-session:\n    payload:\n      secret: "+browser+"\n")
 refs=[{"file":str(p),"format":"yaml","path":keys} for keys in [
  ["refs","API_KEY"],["records","client-connection/browser-session","payload","secret"]]]
 result=redactor_from_references(refs).redact(secret+" "+browser)
 assert result.replacements==2 and secret not in result.text and browser not in result.text

@pytest.mark.parametrize("document",[
 "secret: [SYNTHETIC-YAML-RAW-123456789]",
 "secret: null", "secret: 12345",
 "secret: !!python/object/apply:os.system [SYNTHETIC-YAML-RAW-123456789]",
 "secret: [SYNTHETIC-YAML-RAW-123456789",
 "other: SYNTHETIC-YAML-RAW-123456789"])
def test_yaml_invalid_reference_is_fixed_nonsecret_error(tmp_path,document):
 p=tmp_path/"credentials.yaml";p.write_text(document)
 with pytest.raises(ValueError,match="^REDACTION_REFERENCE_UNAVAILABLE$") as exc:
  redactor_from_references([{"file":str(p),"format":"yaml","path":["secret"]}])
 assert "SYNTHETIC" not in str(exc.value)
