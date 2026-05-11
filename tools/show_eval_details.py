import json

path = r"C:\Users\jahna\Downloads\C5091017 (2)\C5091017\Output\D3\eval\cmp_eval_C5091017.json"
with open(path, "r", encoding="utf-8") as f:
    data = json.load(f)

print("=== Document Score ===")
print(f"  Score: {data.get('document_score')}")
print(f"  Passed: {data.get('document_pass')}")

print("\n=== Metrics ===")
metrics = data.get("metrics", {})
for k, v in metrics.items():
    if isinstance(v, dict):
        print(f"  {k}: score={v.get('score')} target={v.get('target')} pass={v.get('passed')} | {v.get('detail','')}")

# M4 details
m4 = metrics.get("M4_hallucinations", {})
if m4.get("hallucinated_items"):
    print("\n  Hallucinated items:")
    for item in m4["hallucinated_items"]:
        print(f"    {item.get('type')}: {item.get('kri_label', item.get('qtl_name',''))}")
if m4.get("missing_iqmp_items"):
    print(f"\n  Missing IQMP IDs ({m4.get('missing_iqmp_count')}):")
    for item in m4["missing_iqmp_items"]:
        print(f"    {item.get('kri_label')} (GT: {item.get('gt_iqmp')})")

# SS KRI details
print("\n=== SS KRI Details ===")
ss = data.get("study_specific_kris", [])
for kri in (ss if isinstance(ss, list) else ss.get("matched_kris", [])):
    label = kri.get("generated_label", kri.get("ground_truth_label", ""))
    score = kri.get("kri_score", "?")
    status = kri.get("match_status", "?")
    print(f"\n  {label} (score={score}, status={status})")
    attrs = kri.get("attributes", kri.get("attribute_scores", {}))
    for attr_name, attr_val in attrs.items():
        if isinstance(attr_val, dict):
            s = attr_val.get("score", "?")
            jac = attr_val.get("jaccard")
            gen = attr_val.get("generated")
            gen_disp = repr(gen)[:60] if gen else "None"
            line = f"    {attr_name:25s}: {s}"
            if jac is not None:
                line += f"  (jaccard={jac})"
            print(line)

# Global KRI first 3
print("\n=== Global KRI Details (first 3) ===")
gk = data.get("global_kris", [])
gk_list = gk if isinstance(gk, list) else gk.get("matched_kris", [])
for kri in gk_list[:3]:
    label = kri.get("generated_label", kri.get("ground_truth_label", ""))
    score = kri.get("kri_score", "?")
    print(f"\n  {label} (score={score})")
    attrs = kri.get("attributes", kri.get("attribute_scores", {}))
    for attr_name, attr_val in attrs.items():
        if isinstance(attr_val, dict):
            s = attr_val.get("score", "?")
            print(f"    {attr_name:25s}: {s}")
