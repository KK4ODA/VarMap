from varmap.services.updater import helper_script, is_newer, parse_version, pick_asset


def test_version_compare():
    assert parse_version("v0.2.0") == (0, 2, 0)
    assert parse_version("1.10.3-beta") == (1, 10, 3)
    assert is_newer("v0.3.0", "0.2.0") and is_newer("v1.0.0", "0.9.9") and is_newer("0.2.10", "0.2.9")
    assert not is_newer("0.2.0", "0.2.0") and not is_newer("v0.1.9", "0.2.0")


def test_pick_asset(monkeypatch):
    assets = [{"name": "VarMap-0.3.0-linux-x64.tar.gz", "browser_download_url": "l", "size": 1},
              {"name": "VarMap-0.3.0-macos-arm64.tar.gz", "browser_download_url": "m", "size": 2},
              {"name": "VarMap-0.3.0-windows-x64-portable.zip", "browser_download_url": "p", "size": 3},
              {"name": "VarMap-Setup-0.3.0.exe", "browser_download_url": "s", "size": 4},
              {"name": "SHA256SUMS.txt", "browser_download_url": "c", "size": 5}]
    import varmap.services.updater as u
    monkeypatch.setattr(u.sys, "platform", "win32")
    assert pick_asset(assets, "installed")["name"] == "VarMap-Setup-0.3.0.exe"
    assert pick_asset(assets, "portable")["name"] == "VarMap-0.3.0-windows-x64-portable.zip"
    monkeypatch.setattr(u.sys, "platform", "linux")
    assert pick_asset(assets, "source")["name"] == "VarMap-0.3.0-linux-x64.tar.gz"
    assert pick_asset([], "installed") is None


def test_helper_script_waits_installs_relaunches():
    s = helper_script(1234, r"C:\x\VarMap-Setup-0.3.0.exe", r"C:\Program Files\VarMap\VarMap.exe")
    assert 'PID eq 1234' in s and "/VERYSILENT" in s and "/CLOSEAPPLICATIONS" in s
    assert s.index("goto wait") < s.index("VERYSILENT") < s.index('start "" "C:\\Program Files\\VarMap\\VarMap.exe"')
    assert s.rstrip().endswith('(goto) 2>nul & del "%~f0"')     # self-delete must be the final line, via the goto idiom
