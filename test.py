import factorio_rcon
c = factorio_rcon.RCONClient("127.0.0.1", 27015, "secret")
print(c.send_command("/sc rcon.print(game.tick)"))
