@echo off
title Tunel vm-copilot  ^|  abra http://localhost:6081/vnc.html
echo ================================================================
echo   vm-copilot (IP_DA_VM_COPILOT)  -  plataforma COPILOT
echo   Abra no navegador:  http://localhost:6081/vnc.html
echo   (deixe esta janela ABERTA enquanto usa o browser remoto)
echo ================================================================
ssh -i "%USERPROFILE%\.ssh\llmbias_azure" -N ^
    -L 6081:127.0.0.1:6080 -L 5902:127.0.0.1:5901 ^
    -o ServerAliveInterval=30 azureuser@IP_DA_VM_COPILOT
pause
