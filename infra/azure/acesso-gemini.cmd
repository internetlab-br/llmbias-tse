@echo off
title Tunel vm-gemini  ^|  abra http://localhost:6080/vnc.html
echo ================================================================
echo   vm-gemini (IP_DA_VM_GEMINI)  -  plataforma GEMINI
echo   Abra no navegador:  http://localhost:6080/vnc.html
echo   (deixe esta janela ABERTA enquanto usa o browser remoto)
echo ================================================================
ssh -i "%USERPROFILE%\.ssh\llmbias_azure" -N ^
    -L 6080:127.0.0.1:6080 -L 5901:127.0.0.1:5901 ^
    -o ServerAliveInterval=30 azureuser@IP_DA_VM_GEMINI
pause
