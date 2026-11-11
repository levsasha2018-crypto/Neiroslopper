param(
    [Parameter(Mandatory=$true)][string]$InputDir,
    [Parameter(Mandatory=$true)][string]$OutputFile,
    [Parameter(Mandatory=$true)][ValidateSet(32,36,60)][int]$LibraryId
)
# Run with PowerShell 7. The compiler itself is the unmodified IclFileBuilder
# from the official DWIN DGUS V7.651 package (standalone ICLGenerator.dll).
$ErrorActionPreference='Stop'
$libraryPath=$env:DWIN_ICL_GENERATOR
if (!$libraryPath) {throw 'Set DWIN_ICL_GENERATOR to the official DGUS ICLGenerator.dll path'}
$libraryPath=(Resolve-Path -LiteralPath $libraryPath).Path
$asm=[Reflection.Assembly]::LoadFrom($libraryPath)
$global=$asm.GetType('DGUSUtility.Common.GlobalParameters',$true)
$paramType=$asm.GetType('DGUSUtility.Common.GlobalParamType',$true)
$setParam=$global.GetMethod('SetIntParam')
# Keep the Pillow quality-95 4:4:4 JPEG, avoiding a second lossy conversion.
# CpuType=0 selects the conservative T5L1 payload-size limit.
foreach($setting in @{SkipCompression=1;IconQuality=95;CpuType=0;ChromaSubsampling=0}.GetEnumerator()) {
    $enum=[Enum]::Parse($paramType,$setting.Key)
    $setParam.Invoke($null,@($enum,[int]$setting.Value)) | Out-Null
}
$type=$asm.GetType('DGUSCommon.Tools.IclFileBuilder',$true)
$ctor=$type.GetConstructor([type[]]@([uint16],[byte]))
$builder=$ctor.Invoke(@([uint16]1023,[byte]0))
$type.GetProperty('FileId').SetValue($builder,$LibraryId)
$add=$type.GetMethod('AddFile',[type[]]@([uint16],[byte[]]))
foreach($file in @(Get-ChildItem -LiteralPath $InputDir -File | Sort-Object Name)) {
    if($file.Name -notmatch '^(\d+).*\.jpg$') { throw ('Expected numbered JPEG: '+$file.Name) }
    $id=[uint16]$matches[1]
    $bytes=[IO.File]::ReadAllBytes($file.FullName)
    $result=$add.Invoke($builder,@($id,$bytes))
    if($result -ne 0) { throw ('DWIN AddFile failed '+$file.Name+' code '+$result) }
}
$save=$type.GetMethod('Save',[type[]]@([string]))
$result=$save.Invoke($builder,@([IO.Path]::GetFullPath($OutputFile)))
if($result -ne 0) { throw ('DWIN Save failed code '+$result+' source '+$type.GetProperty('LastErrorFilePath').GetValue($builder)) }
Write-Output ('Built official DGUS III ICL: '+$OutputFile)
Get-Item -LiteralPath $OutputFile | Select-Object Length,FullName
Get-FileHash -LiteralPath $OutputFile -Algorithm SHA256
