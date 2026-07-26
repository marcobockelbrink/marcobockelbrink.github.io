#Requires -Modules VMware.PowerCLI
<#
  Kapa-Dashboard: Offline-Export eines vCenters ohne vROps-Anbindung.

  Aufruf (auf einem Rechner mit Netz zum isolierten vCenter):
    .\kapa_export.ps1 -Server vcenter.insel.local
    .\kapa_export.ps1 -Server vcenter.insel.local -Cluster "Prod*" -OutFile insel.json

  Ergebnis: eine JSON-Datei, die sich im Dashboard unter
  Verwaltung -> Import mit einem frei gewaehlten Quellnamen hochladen laesst.
  Erfasst je Cluster: ESXi-Hosts (Cores/RAM), VMs (vCPU/RAM/Power),
  Datastores (Groesse/Belegung/Typ) und Portgruppen mit VLAN-ID.
#>
param(
    [Parameter(Mandatory = $true)][string]$Server,
    [string]$Cluster = "*",
    [string]$OutFile = "kapa_import.json"
)

Connect-VIServer -Server $Server | Out-Null

$result = @()
foreach ($cl in (Get-Cluster -Name $Cluster | Sort-Object Name)) {
    Write-Host "Lese Cluster $($cl.Name) ..."
    $vmhosts = @($cl | Get-VMHost)

    $hosts = @()
    foreach ($h in $vmhosts) {
        $hosts += [ordered]@{
            name   = $h.Name
            cores  = [int]$h.NumCpu
            ram_gb = [math]::Round($h.MemoryTotalGB, 1)
        }
    }

    $vms = @()
    foreach ($vm in ($cl | Get-VM)) {
        $vms += [ordered]@{
            name   = $vm.Name
            vcpu   = [int]$vm.NumCpu
            ram_gb = [math]::Round($vm.MemoryGB, 1)
            on     = ($vm.PowerState -eq "PoweredOn")
        }
    }

    $ds = @()
    foreach ($d in ($cl | Get-Datastore)) {
        $ds += [ordered]@{
            name    = $d.Name
            type    = "" + $d.Type
            cap_gb  = [math]::Round($d.CapacityGB, 1)
            used_gb = [math]::Round(($d.CapacityGB - $d.FreeSpaceGB), 1)
        }
    }

    # Portgruppen: Standard-vSwitches je Host + dvSwitches der Cluster-Hosts.
    # Es reicht die VLAN-ID (einzeln oder Bereich) - keine weiteren Details.
    $pgs = @{}
    foreach ($pg in ($vmhosts | Get-VirtualPortGroup -Standard -ErrorAction SilentlyContinue)) {
        $pgs[$pg.Name] = "" + $pg.VLanId
    }
    $vds = @($vmhosts | Get-VDSwitch -ErrorAction SilentlyContinue | Sort-Object -Unique Name)
    foreach ($sw in $vds) {
        foreach ($pg in (Get-VDPortgroup -VDSwitch $sw -ErrorAction SilentlyContinue)) {
            $v = $pg.VlanConfiguration
            if ($null -ne $v -and $null -ne $v.VlanId) {
                $pgs[$pg.Name] = "" + $v.VlanId
            } elseif ($null -ne $v -and $v.Ranges) {
                $pgs[$pg.Name] = (@($v.Ranges | ForEach-Object {
                    "$($_.StartVlanId)-$($_.EndVlanId)" }) -join ",")
            } elseif (-not $pgs.ContainsKey($pg.Name)) {
                $pgs[$pg.Name] = ""
            }
        }
    }
    $pglist = @()
    foreach ($k in ($pgs.Keys | Sort-Object)) {
        $pglist += [ordered]@{ name = $k; vlan = $pgs[$k] }
    }

    $result += [ordered]@{
        name       = $cl.Name
        hosts      = $hosts
        vms        = $vms
        datastores = $ds
        portgroups = $pglist
    }
}

[ordered]@{ clusters = $result } | ConvertTo-Json -Depth 6 |
    Out-File -FilePath $OutFile -Encoding UTF8
Write-Host "Fertig: $OutFile ($($result.Count) Cluster)"
