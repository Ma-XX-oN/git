using System.IO.Pipes;

namespace Preload.Client
{
  /// <summary>Shared native pipe connection operations for the PowerShell frontend.</summary>
  public static class PipeConnector
  {
    /// <summary>Open an owned asynchronous duplex pipe within five seconds.</summary>
    /// <param name="name">Pipe name without the Windows pipe namespace prefix.</param>
    /// <returns>A connected stream that the caller must dispose.</returns>
    /// <exception cref="System.TimeoutException">The connection timed out.</exception>
    /// <exception cref="System.IO.IOException">The pipe could not be connected.</exception>
    public static NamedPipeClientStream Open(string name)
    {
      var pipe = new NamedPipeClientStream(
        ".", name, PipeDirection.InOut, PipeOptions.Asynchronous);
      try
      {
        pipe.Connect(5000);
        return pipe;
      }
      catch
      {
        pipe.Dispose();
        throw;
      }
    }
  }
}
