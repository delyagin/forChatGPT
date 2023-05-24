#Global version
#2021-10-28 by Yelagin Aleksej for MUA VM agent
#powershell -ExecutionPolicy Bypass -File "\\file-server\B-Test\AElagin\Multi-Access\In_domain\client2-agent.ps1"
#Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass

Write-host "Ver. 2022-12-09 "
$SqlServer = "SQLVM\SQL";
$SqlDB = "MUA-DB";
$SqlLogin = "tester";
$SqlPassw = "12345"
$hostName=$env:COMPUTERNAME
$clientsList = new-object 'System.Collections.Generic.List[string]'
$list = new-object 'System.Collections.Generic.List[string]'
Write-Host $hostName
#$global:scriptRunning=$false
$global:currentJobRunning=$false
$repositoryPath="C:\Testbot-Repos\testreposerver--URS-Test\URS-Test"
$descriptionPath = "C:\Program Files\Ultimate Risk Solutions\URS Application Studio\Bin\Description.txt"
$get_tables_query = '$queryText = "SELECT TABLE_NAME FROM [MUA-DB].INFORMATION_SCHEMA.TABLES WHERE TABLE_TYPE=''BASE TABLE''"'
$get_status_query = '$queryText ="SELECT status FROM $table WHERE id=$value"'
$change_status_query = '$queryText ="UPDATE $table SET status=''$status'' WHERE id=$value;"'
$get_revision_query = '$queryText ="SELECT revision FROM $table WHERE id=$value"'

$get_url = '$queryText ="SELECT repository FROM $table WHERE id=$value;"'

$get_build_path = '$queryText ="SELECT build_path FROM $table WHERE id=$value;"'
$get_client = '$queryText ="SELECT client FROM $table WHERE id=$value;"'
$get_script_name = '$queryText ="SELECT script_name FROM $table WHERE id=$value;"'
$get_id = '$queryText ="SELECT ID FROM $table;"'
function run-test($script_info, $client_serial_number){
     #process{
         $attempts=5
         $info = $script_info.Split("-")
         $function=$info[1]
         Write-Host "client_serial_number: " $client_serial_number -ForegroundColor Green
         Write-Host "script_info: " $script_info -ForegroundColor Green
         $module = ($info[0] + $client_serial_number).TrimStart().TrimEnd()
         #Write-Host "module: \'$module\'" -ForegroundColor Yellow
         $qem_module = "scripts.ayelagin.multi_user.socket.quick_edit_mode"
         $qem_func = "to_disable"
         kill_re_process
         python "C:\Testbot-Repos\testreposerver--URS-Test\URS-Test\run-tests.py" --run-test $qem_module $qem_func
         python "C:\Testbot-Repos\testreposerver--URS-Test\URS-Test\run-tests.py" --run-test $module $function

         <#for(($i=1); $i -ilt $attempts;$i++){
             try {
                    Write-Host "module:" $module -ForegroundColor Yellow
                    python "C:\Testbot-Repos\testreposerver--URS-Test\URS-Test\run-tests.py" --run-test $module $function
                    return $true
                    #$global:runStatus=$false
             }
             catch {
                write-host "Attempt=" $i -BackgroundColor Green
                if($i -ilt $attempts) { sleep 2; continue }
                else {throw "Permission denied: $module" }
                break
             }
           }
        }#>
      $currentTime= now
      write-host "[$currentTime]: Python process finished"
}

function get_local_build($path){
  if(Test-Path -Path $path){
     $file_data = (Get-Content $path).Trim() #Removes all leading and trailing white-space
     return $file_data
   }
   else{
        Write-Host "Incorrect path: $path" -ForegroundColor Red
        return $false
   }

}

function local_build_path(){
    $path_12x = "\\file-server\URS Application Studio Setup\Version 12\x64\URSApplicationStudio-"
    $path_dlls = "\\file-server\URS Application Studio Setup\Version 12\dlls\x64\URSApplicationStudio-"
    $data = get_local_build $descriptionPath
    $length = $path.Length
    $res_path = $path_12x + $data + ".msi"
    if (Test-Path -Path $res_path) { return $res_path }
    else {
        return $path_dlls + $data + ".msi"
    }
}

function check_build($build_path){
   if (Test-Path -Path $descriptionPath){
       $local_build = get_local_build $descriptionPath
       Write-Host "local build: $local_build"
       Write-Host "  new build: $build_path"
       if ($build_path.Contains($local_build)){ return $true }
       else{ return $false}
    }
    else { return $false } 
}

function msi_uninstall_local($path){
        try{
            if (Test-Path -Path $descriptionPath){
                $currentTime = now
                Write-Host "[$currentTime]: RE uninstalling ..."
                $msi_path = local_build_path
                $pathCorrect = Test-Path -Path $msi_path
                if(-not $pathCorrect){
                    Write-Host "[$currentTime]: MSI path is not correct $msi_path" -ForegroundColor Red
                     -ForegroundColor Red
                }
                Write-Host "[$currentTime]: MSI to uninstall: " $msi_path
                $arguments = "/x `"$msi_path`" /quiet"
                Start-Process msiexec.exe -ArgumentList $arguments -Wait -Verb RunAs -ErrorAction Stop
                Write-Host "[$currentTime]: RE uninstalling complete "
            }
        }
        catch{Write-Host "[$currentTime]: Risk Explorer not uninstalled"}
}

function kill_re_process(){
    try{
            Write-Host "[$currentTime]: Kill process 'RiskExplorer'"
            Stop-Process -Name "RiskExplorer" -Force -ErrorAction Stop
         }
   catch{
             Write-Host "No 'RiskExplorer' process to kill'"
         }
}

function msi_install($path){
   $currentTime = now
   if( -not (check_build $path)){
        try{
            Write-Host "[$currentTime]: Kill process 'RiskExplorer'"
            Stop-Process -Name "RiskExplorer" -Force -ErrorAction Stop
         }
         catch{
             Write-Host "No 'RiskExplorer' process to kill'"
         }
        msi_uninstall_local $path
        Write-Host "[$currentTime]: RE installing ..."
        $arguments = "/i `"$path`" /quiet"
        Start-Process msiexec.exe -ArgumentList $arguments -Wait -Verb RunAs
        Write-Host "[$currentTime]: Installation completed"
   }
   else{  Write-Host "[$currentTime]: --- Necessary build has already installed ---"; return $true }
}

function update_repository (){
    process{
            Write-Host "Update repository..."
            Set-Location $repositoryPath
            hg pull --update
            $success = $?
            write-host "success:" $success
            if($success){
                hg update $revision
                Write-Host "Update completed"
            }
            else{
                write-host "Use hg recover"
                hg recover
                hg pull --update
                hg update
                Write-Host "Update after 'hg recover' completed"
            }
         }
}
function run-sql($query, $value, $table, $status){
    $list.Clear()
    $SqlConnection = New-Object System.Data.SqlClient.SqlConnection
    $SqlConnection.ConnectionString = "Server=$SqlServer; Database=$SqlDB; User ID=$SqlLogin; Password=$SqlPassw;"
    try{
        $SqlConnection.Open()
        $SqlCmd = $SqlConnection.CreateCommand()
        Invoke-Expression $query
        $SqlCmd.CommandText = $queryText
        $objReader = $SqlCmd.ExecuteReader()
        while ($objReader.read()) {
          $list.Add($objReader.GetValue(0))
          #write-host "---: " $objReader.GetValue(0)
        }
          $objReader.close()
          $SqlConnection.close()
          return $list
    }
    catch{
         try{
                $objReader.close()
                $SqlConnection.close()
                #write-host "Catch1 --- return 0 ---"
                return 0
           }
        catch{
                 #write-host "Catch2 --- return 0 ---"
                 return 0
             }
    }
 }
$tables = run-sql $get_tables_query '0' '0'
Write-Host "tables: $tables"
foreach ($table in $tables){
    if ($table.StartsWith('mclient')){
        #write-host "table: " $table
        $clientsList.Add($table)
       }
}
Write-Host "clients: $clientsList"
function now(){
    $nowDateTime = (Get-Date -Format "MM/dd/yyyy HH:mm")
    $convertTimeZone = [System.TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([datetime]$nowDateTime,"FLE Standard Time")
    $currentTime=$convertTimeZone.ToString("MM/dd/yyyy HH:mm")
    return $currentTime
}

function remove_ini_file() {
    $users = "lob_actuary1", "lob_actuary2", "lob_actuary3", "lob_actuary4", "lob_actuary5", "lob_actuary6",`
           "lob_actuary7", "lob_actuary8", "re_actuary1", "re_actuary2","re_actuary3", `
           "re_actuary4", "lob_manager1", "lob_manager2", "lob_manager3", "prj_administrator",`
           "re_administrator", "re_manager", "re_owner"
    foreach ($user in $users){
      try {
        $path_to_ini_file = Join-Path -Path C:\Users\$user -ChildPath "AppData\Roaming\Ultimate Risk Solutions\RiskExplorer\Risk Explorer-Unicode.ini"
        Remove-item $path_to_ini_file -ErrorAction Stop
        Write-Host "'.ini' file has been removed"
      } catch {
      Write-Host "No file to remove"
      }
    }
}
function copy_ini_file(){
     $users = "lob_actuary1", "lob_actuary2", "lob_actuary3", "lob_actuary4", "lob_actuary5", "lob_actuary6",`
           "lob_actuary7", "lob_actuary8", "re_actuary1", "re_actuary2","re_actuary3", `
           "re_actuary4", "lob_manager1", "lob_manager2", "lob_manager3", "prj_administrator",`
           "re_administrator", "re_manager", "re_owner"
     foreach ($user in $users){
      try {
        $path_to_copy = Join-Path -Path C:\Users\$user -ChildPath "AppData\Roaming\Ultimate Risk Solutions\RiskExplorer"
        Copy-Item "\\DOMAIN16\MUA\files\Risk Explorer-Unicode.ini" -Destination $path_to_copy -ErrorAction Stop
        Write-Host "'.ini' file has been copied"
      } catch {
      Write-Host "No file to copy" -ForegroundColor Red
      }
    }
}
function run-job(){
    #write-host "RUN Job"
    $repoNeedUpdate=$false
    foreach($client in $clientsList){
        if($client -ieq $hostName ){
            $id_list = run-sql $get_id '0' $client
            #write-host "id_list: $id_list"
            if($id_list){
            foreach ($id in $id_list){
                $currentTime= now
                $status = run-sql $get_status_query $id $client
                if ($status.StartsWith("new") -and ($global:currentJobRunning -eq $false)){
                    #cls
                    #sleep 1
                    $repoNeedUpdate=$true
                    write-host "[$currentTime]: Change status to 'running'"
                    run-sql $change_status_query $id $client 'running'
                    $revision = run-sql $get_revision_query $id $client
                    $repoURL = run-sql $get_url $id $client
                    $buildPath= run-sql $get_build_path $id $client
                    $script_name = run-sql $get_script_name $id $client
                    $clientNumber = run-sql $get_client $id $client
                    $global:currentJobRunning=$true
                    Write-Host "build_path: " $buildPath
                    Write-Host "revision: " $revision
                    Write-Host "script_name: " $script_name
                    Write-Host "clientNumber: " $clientNumber
                    msi_install $buildPath
                    update_repository
                    remove_ini_file
                    copy_ini_file
                    run-test $script_name $clientNumber
                 }
                 elseif($status.StartsWith("running") -and ($global:currentJobRunning)){
                    Write-Host "[$currentTime]: Change status to 'done'"
                    run-sql $change_status_query $id $client 'done'
                 }
                 elseif($status.StartsWith("done") -and ($global:currentJobRunning)){
                    #$global:runStatus=$true
                    #Write-Host "Change runStatus to "$global:runStatus
                    run-sql $change_status_query $id $client 'toDelete'
                    write-host "[$currentTime]: Change status to 'toDelete'"
                    $global:currentJobRunning=$false
                 }
                 elseif($global:currentJobRunning){
                    Write-Host "[$currentTime]: Job has been deleted. Ready for new job"
                    $global:currentJobRunning = $false
                 }
               <#if($global:currentJobRunning){
                    Write-Host "Run new job"
                    update_repository
                   # $global:scriptRunning=$true
                    run-test $script_name $clientNumber
               }#>
          # if($repoNeedUpdate){
               # update_repository
              #  $repoNeedUpdate=$false
           #}
          }
         }
        }
    }
}
# & {subst T: C:\Testbot-Repos\testreposerver--URS-Test}
# while(1){
#     #write-host "===== Parsing files for job ====="
#     run-job
#     sleep 5
# }
