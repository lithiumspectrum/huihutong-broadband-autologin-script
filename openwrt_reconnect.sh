#!/bin/sh
# 慧湖通门户：人工执行一次检测/认证，不启动驻留进程。
# 安装：cp openwrt_reconnect.sh /usr/bin/reconnect && chmod 755 /usr/bin/reconnect

PROG_DIR=${PORTAL_LOGIN_HOME:-/root/portal_login}
export PYTHONPATH="$PROG_DIR"

/usr/bin/python3 -m portal_login once
rc=$?
if [ "$rc" -eq 0 ]; then
	echo "reconnect: 网络在线（或本次重连已成功）"
else
	echo "reconnect: 本次未恢复网络（退出码 $rc），可稍后重试" >&2
fi
exit "$rc"
