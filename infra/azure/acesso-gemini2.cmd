@echo off
title Tunel vm-gemini2  ^|  abra http://localhost:6082/vnc.html
echo ================================================================
echo   vm-gemini2 (IP_DA_VM_GEMINI2)  -  GEMINI, 2a conta (eixo integridade)
echo   Abra no navegador:  http://localhost:6082/vnc.html
echo   (deixe esta janela ABERTA enquanto usa o browser remoto)
echo ================================================================
ssh -i "%USERPROFILE%\.ssh\llmbias_azure" -N ^
    -L 6082:127.0.0.1:6080 -L 5903:127.0.0.1:5901 ^
    -o ServerAliveInterval=30 azureuser@IP_DA_VM_GEMINI2
pause
