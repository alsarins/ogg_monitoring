#   !/bin/bash
#   wrapper script for OGG monitoring: looking for mgr or ServiceManager processes and run python script against it
#   Author: Mamaev Sergei <smamaev@mail.ru>
#   TODO: add locking mechanism for single run
PLATFORM=$(uname)
ZABBIX_SCRIPT="/path/to/ogg_monitor.py"
ZABBIX_SCRIPT_CONF="/path/to/ogg_monitor.ini"
USERNAME=$(whoami)
#   search for running ogg and start monitoring script
for ogg_process in $(ps -fu ${USERNAME} | egrep "mgr PARAMFILE|ServiceManager" | grep -v grep | awk '{print $2}')
do
    if [[ "${PLATFORM}" == "AIX" ]]; then
        running_oracle_home=`ps eww ${ogg_process} | tr " " "\n" | grep ORACLE_HOME= | awk -F'=' '{print $2}'`
        ogg_home=`procwdx ${ogg_process} | awk '{print $2}'`
        ogg_var_home=`ps eww ${ogg_process}| tr " " "\n" | grep OGG_VAR_HOME= | awk -F'=' '{print $2}'`
        ogg_ldpath=`ps eww ${ogg_process}| tr " " "\n" | grep LD_LIBRARY_PATH= | awk -F'=' '{print $2}'`
        ogg_libpath=`ps eww ${ogg_process}| tr " " "\n" | grep LIBPATH= | awk -F'=' '{print $2}'`
        ogg_env="OGG_HOME=${ogg_home} ORACLE_HOME=${running_oracle_home} OGG_VAR_HOME=${ogg_var_home} LD_LIBRARY_PATH=${ogg_ldpath} LIBPATH=${ogg_libpath}"
    elif [[ "${PLATFORM}" == "SunOS" ]]; then
        running_oracle_home=`pargs -e ${ogg_process} | grep ORACLE_HOME | awk -F'=' '{print $2}'`
        ogg_home=`pwdx ${ogg_process} | awk '{print $2}'`
        ogg_var_home=`pargs -e ${ogg_process} | grep OGG_VAR_HOME | awk -F'=' '{print $2}'`
        ogg_ldpath=`pargs -e ${ogg_process} | grep LD_LIBRARY_PATH | awk -F'=' '{print $2}'`
        ogg_env="OGG_HOME=${ogg_home} ORACLE_HOME=${running_oracle_home} OGG_VAR_HOME=${ogg_var_home} LD_LIBRARY_PATH=${ogg_ldpath}"
    elif [[ "${PLATFORM}" == "Linux" ]]; then
        running_oracle_home=`cat /proc/${ogg_process}/environ | tr '\0' '\n' | grep ORACLE_HOME= | awk -F'=' '{print $2}'`
        ogg_home=`pwdx ${ogg_process} | awk '{print $2}'`
        ogg_var_home=`cat /proc/${ogg_process}/environ | tr '\0' '\n' | grep OGG_VAR_HOME= | awk -F'=' '{print $2}'`
        ogg_ldpath=`cat /proc/${ogg_process}/environ | tr '\0' '\n' | grep LD_LIBRARY_PATH= | awk -F'=' '{print $2}'`
        ogg_env="OGG_HOME=${ogg_home} ORACLE_HOME=${running_oracle_home} OGG_VAR_HOME=${ogg_var_home} LD_LIBRARY_PATH=${ogg_ldpath}"
    elif [[ "${PLATFORM}" == "Windows" ]]; then
        echo "Not implemented for Windows yet. Exiting"
    exit 1
    fi
export ${ogg_env}; ${ZABBIX_SCRIPT} -c ${ZABBIX_SCRIPT_CONF} &
