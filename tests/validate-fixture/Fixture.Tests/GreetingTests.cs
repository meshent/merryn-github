namespace Fixture.Tests;

public class GreetingTests
{
    [Xunit.Fact]
    public void Greets_by_name() => Xunit.Assert.Equal("Hello, Ada", Greeting.For("Ada"));
}
