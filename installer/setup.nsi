; setup.nsi - Enterprise Ontology Platform Windows installer (ASCII only; Chinese strings live in PS-generated .bat/.lnk)
!define PRODUCT_NAME "Enterprise Ontology Platform"
!define PRODUCT_VERSION "1.0.2"
!define PRODUCT_PUBLISHER "lifengss"
!define UNINST_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\${PRODUCT_NAME}"

Unicode true
Name "${PRODUCT_NAME} ${PRODUCT_VERSION}"
OutFile "dist\OntologyPlatform-Setup-${PRODUCT_VERSION}.exe"
; InstallDir: registry first (keeps the original path on upgrade/reinstall,
; compatible with older Program Files installs); otherwise default to the
; current user's LOCALAPPDATA\Programs (2026-10-30: on some machines file
; creation under the protected Program Files tree is blocked / permission
; denied and the install fails; users may still pick Program Files manually)
InstallDir "$LOCALAPPDATA\Programs\OntologyPlatform"
InstallDirRegKey HKLM "${UNINST_KEY}" "InstallDir"
RequestExecutionLevel admin
SetCompressor /SOLID lzma

Page directory
Page instfiles
UninstPage uninstConfirm
UninstPage instfiles

Section -install
  SectionIn RO
  SetOutPath "$INSTDIR"
  File /r "payload\*.*"

  WriteUninstaller "$INSTDIR\Uninstall.exe"
  WriteRegStr HKLM "${UNINST_KEY}" "DisplayName" "${PRODUCT_NAME}"
  WriteRegStr HKLM "${UNINST_KEY}" "DisplayVersion" "${PRODUCT_VERSION}"
  WriteRegStr HKLM "${UNINST_KEY}" "Publisher" "${PRODUCT_PUBLISHER}"
  WriteRegStr HKLM "${UNINST_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKLM "${UNINST_KEY}" "DisplayIcon" "$INSTDIR\start-platform.bat"
  WriteRegStr HKLM "${UNINST_KEY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr HKLM "${UNINST_KEY}" "InstallDir" "$INSTDIR"

  MessageBox MB_OK "Platform files have been deployed to the target directory.$\r$\nNext: auto port detection/re-assignment and dependency provisioning (JDK / Python / Postgres / Neo4j).$\r$\nThis may take 3-10 minutes. NOTE: this installer window will APPEAR FROZEN while the$\r$\nblack command console runs - that is normal, watch the console for progress.$\r$\nIf it hangs for more than ~10 minutes you may close the console window and check the log.$\r$\nLog: <install dir>\logs\install.log"

  ExecWait 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$INSTDIR\scripts\install-deps.ps1" -InstallDir "$INSTDIR"'
  ; Success/failure is decided by the result file written by the script (positive confirmation):
  ; Status=OK only when the whole provisioning run truly finished. A missing file means the
  ; console was closed / the script crashed - treat as failure in all non-OK cases.
  ReadINIStr $R0 "$INSTDIR\logs\install-result.ini" "Result" "Status"
  StrCmp $R0 "OK" .depOk
  ReadINIStr $R1 "$INSTDIR\logs\install-result.ini" "Result" "Message"
  StrCmp $R1 "" .depNoResult
  MessageBox MB_ICONSTOP "Install configuration FAILED.$\r$\n$R1$\r$\nSee log: <install dir>\logs\install.log$\r$\nRetry: close any leftover console/installer processes, then re-run$\r$\n<install dir>\scripts\install-deps.ps1 as admin. Or uninstall first (<install dir>\Uninstall.exe)$\r$\nand run this setup again."
  Goto .installEnd
.depNoResult:
  MessageBox MB_ICONSTOP "Install configuration did NOT finish (no result reported - the console window may have been closed, or the script crashed).$\r$\nSee log: <install dir>\logs\install.log$\r$\nRetry: close any leftover console/installer processes, then re-run$\r$\n<install dir>\scripts\install-deps.ps1 as admin. Or uninstall first (<install dir>\Uninstall.exe)$\r$\nand run this setup again."
  Goto .installEnd
.depOk:
  MessageBox MB_ICONINFORMATION "Installation completed.$\r$\nNote: ports may have been re-assigned automatically if defaults were busy.$\r$\nEntry URLs: see <install dir>\scripts\runtime-config.json$\r$\nStart/Stop: desktop shortcuts (Chinese names) or start-platform.bat / stop-platform.bat"
.installEnd:
SectionEnd

Section "Uninstall"
  ; stop services best-effort (ignore failure)
  ExecWait 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$INSTDIR\scripts\stop-services.ps1" -InstallDir "$INSTDIR"'
  ; database/data OUTSIDE the install dir: ask the user (backup recommended before delete)
  ; NOTE: $ALLUSERSPROFILE / $COMMONAPPDATA are NOT built-in NSIS vars in v3.12
  ; (verified: they pass through as literals). Read ProgramData from registry instead.
  ReadRegStr $R0 HKLM "SOFTWARE\Microsoft\Windows\CurrentVersion" ProgramData
  StrCmp $R0 "" 0 +2
  StrCpy $R0 "C:\ProgramData"
  IfFileExists "$R0\OntologyPlatform" 0 .skipDbAsk
  MessageBox MB_YESNO|MB_ICONQUESTION "A database and data directory lives OUTSIDE the install directory:$\r$\n$\r$\n  $R0\OntologyPlatform$\r$\n$\r$\nIt contains PostgreSQL data and logs, and Neo4j temp files. The Neo4j graph data and the Fuseki dataset inside the install directory are removed with the install directory in any case.$\r$\n$\r$\nIf you still need the data, back it up first.$\r$\n$\r$\n[Yes] delete it    [No] keep it" IDNO .skipDbAsk
  RMDir /r "$R0\OntologyPlatform"
  IfErrors 0 +2
  MessageBox MB_ICONSTOP "The data directory could not be fully removed (some files may be in use). Please check and delete manually:$\r$\n$R0\OntologyPlatform"
.skipDbAsk:
  ; shortcuts: primary = actual recorded path (may have fallen back to public desktop / install dir)
  ReadINIStr $R2 "$INSTDIR\scripts\install-config.ini" "Shortcuts" "StartLnkPath"
  IfFileExists "$R2" 0 +2
  Delete "$R2"
  ReadINIStr $R2 "$INSTDIR\scripts\install-config.ini" "Shortcuts" "StopLnkPath"
  IfFileExists "$R2" 0 +2
  Delete "$R2"
  ; fallbacks by name: user desktop + public desktop (older builds only recorded names)
  ReadINIStr $R0 "$INSTDIR\scripts\install-config.ini" "Shortcuts" "StartLnk"
  ReadINIStr $R1 "$INSTDIR\scripts\install-config.ini" "Shortcuts" "StopLnk"
  IfFileExists "$DESKTOP\$R0" 0 +2
  Delete "$DESKTOP\$R0"
  IfFileExists "$DESKTOP\$R1" 0 +2
  Delete "$DESKTOP\$R1"
  IfFileExists "C:\Users\Public\Desktop\$R0" 0 +2
  Delete "C:\Users\Public\Desktop\$R0"
  IfFileExists "C:\Users\Public\Desktop\$R1" 0 +2
  Delete "C:\Users\Public\Desktop\$R1"
  RMDir /r "$INSTDIR"
  DeleteRegKey HKLM "${UNINST_KEY}"
  ; Leftover feedback (2026-10-30 remote machine report: install dir not fully
  ; removed after uninstall with no notice at all) - usually files still in use
  ; (console/IDE holding files, services not fully stopped): tell the user to
  ; clean up manually or after a reboot
  IfFileExists "$INSTDIR" 0 .noLeftover
  MessageBox MB_ICONEXCLAMATION "The install directory could not be fully removed (some files may still be in use):$\r$\n$INSTDIR$\r$\n$\r$\nClose any console/IDE windows holding files in it (and make sure all platform services are stopped), then delete the folder manually - or delete it after a reboot."
.noLeftover:
SectionEnd
