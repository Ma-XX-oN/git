using System.IO;
using System.IO.Pipes;
using System.Threading.Tasks;

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

    /// <summary>
    /// Copy caller input to a worker's stdin channel, then close it so the command
    /// reads end-of-file. Runs concurrently with output draining, so input and
    /// output larger than the pipe buffers cannot deadlock.
    /// </summary>
    /// <param name="source">Owned input stream; disposed when the copy ends.</param>
    /// <param name="destination">Owned stdin channel; disposed when the copy ends.</param>
    /// <returns>A task that completes once both streams are disposed.</returns>
    /// <remarks>
    /// A command that exits without reading all its input breaks the pipe. That
    /// ends the copy quietly, as for a native command; the exit status still
    /// comes from the worker.
    /// </remarks>
    public static async Task Feed(Stream source, Stream destination)
    {
      try
      {
        await source.CopyToAsync(destination).ConfigureAwait(false);
        await destination.FlushAsync().ConfigureAwait(false);
      }
      catch (IOException)
      {
        // The command closed its stdin before reading everything.
      }
      finally
      {
        destination.Dispose();
        source.Dispose();
      }
    }
  }
}
